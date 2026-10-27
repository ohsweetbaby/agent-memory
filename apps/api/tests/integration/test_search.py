"""四路召回检索：融合、降级、作用域隔离与时间过滤。

用例按任务简报的 6 条场景组织。数据直接落 Postgres（事实源），再用
``QdrantIndex`` 写入与查询向量一致的 point；图通道用记录型替身承接，
避免为每条检索用例启动 Neo4j 容器（真实图投影在 ``test_neo4j.py``）。
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import timedelta
from typing import Any, Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.embeddings import FakeEmbedding
from app.adapters.llm import NoopReranker
from app.adapters.qdrant import QdrantIndex
from app.jobs.runtime import Runtime, configure_runtime, reset_runtime
from app.models import Entity, Memory, MemoryEntity, Workspace
from app.models.base import utcnow

pytestmark = pytest.mark.integration

WORKSPACE_ID = "ws1"
TARGET_CONTENT = "用户对花生过敏"


class RecordingGraph:
    """Neo4j 通道替身：只返回预先登记的 1–2 跳结果。"""

    def __init__(self) -> None:
        self.results: list[str] = []

    def expand(self, entity_ids, hops: int = 2, limit: int = 30) -> list[str]:
        if not entity_ids:
            return []
        return list(self.results)[:limit]

    def close(self) -> None:  # pragma: no cover - 运行时关闭钩子
        pass


@pytest.fixture()
def fake_embedding() -> FakeEmbedding:
    return FakeEmbedding(dim=8)


@pytest.fixture()
def graph_index() -> RecordingGraph:
    return RecordingGraph()


@pytest.fixture(autouse=True)
def search_runtime(
    session_factory: sessionmaker[Session],
    fake_embedding: FakeEmbedding,
    qdrant_index: QdrantIndex,
    graph_index: RecordingGraph,
) -> Iterator[None]:
    """把 API 检索依赖指向当前测试事务、真实 Qdrant 与图替身。"""
    configure_runtime(
        Runtime(
            session_scope=lambda: nullcontext(session_factory()),
            embedding=fake_embedding,
            qdrant=qdrant_index,
            neo4j=graph_index,
        )
    )
    try:
        yield
    finally:
        reset_runtime()


def _new_memory(
    session: Session,
    *,
    content: str,
    importance: float,
    scope: str = "user",
    scope_id: str | None = "u1",
    memory_type: str = "semantic",
    metadata: dict[str, Any] | None = None,
    valid_to=None,
) -> Memory:
    memory = Memory(
        workspace_id=WORKSPACE_ID,
        scope=scope,
        scope_id=scope_id,
        type=memory_type,
        content=content,
        importance=importance,
        confidence=0.9,
        stability=1.0,
        status="active",
        source_type="message",
        source_id="msg-1",
        metadata_=dict(metadata or {"source_quote": "我吃花生会过敏"}),
        valid_from=utcnow() - timedelta(days=3),
        valid_to=valid_to,
        last_reinforced_at=utcnow() - timedelta(days=1),
    )
    session.add(memory)
    session.flush()
    return memory


def _index(
    qdrant: QdrantIndex,
    memory: Memory,
    vector: list[float],
    entity_ids: list[str] | None = None,
) -> None:
    qdrant.upsert(
        memory.id,
        vector,
        {
            "workspace_id": memory.workspace_id,
            "scope": memory.scope,
            "scope_id": memory.scope_id,
            "type": memory.type,
            "status": memory.status,
            "importance": memory.importance,
            "valid_to": memory.valid_to,
            "last_reinforced_at": memory.last_reinforced_at,
            "entity_ids": entity_ids or [],
            "version": memory.version,
        },
    )


@pytest.fixture()
def seeded_memories(
    session_factory: sessionmaker[Session],
    qdrant_index: QdrantIndex,
    fake_embedding: FakeEmbedding,
    graph_index: RecordingGraph,
) -> dict[str, str]:
    """ws1 的基础语料：目标事实、干扰项、global 事实与已过期历史。"""
    query_vector = fake_embedding.embed_sync(["花生过敏"])[0]
    with session_factory() as session:
        if session.get(Workspace, WORKSPACE_ID) is None:
            session.add(Workspace(id=WORKSPACE_ID, name="测试工作区"))
            session.flush()

        target = _new_memory(
            session,
            content=TARGET_CONTENT,
            importance=0.9,
        )
        other = _new_memory(
            session,
            content="用户喜欢喝绿茶",
            importance=0.6,
        )
        global_memory = _new_memory(
            session,
            content="工作区默认语言是中文",
            importance=0.4,
            scope="global",
            scope_id=None,
        )
        expired = _new_memory(
            session,
            content="北京",
            importance=0.3,
            valid_to=utcnow() - timedelta(days=1),
        )
        entity = Entity(workspace_id=WORKSPACE_ID, name="花生", type="food")
        session.add(entity)
        session.flush()
        session.add(
            MemoryEntity(memory_id=target.id, entity_id=entity.id, role="object")
        )
        session.commit()

        ids = {
            "target": target.id,
            "other": other.id,
            "global": global_memory.id,
            "expired": expired.id,
        }

    # 目标事实的向量与 query 完全一致，模拟语义模型的高相似度召回。
    _index(qdrant_index, target, query_vector, [entity.id])
    _index(qdrant_index, other, fake_embedding.embed_sync([other.content])[0])
    _index(
        qdrant_index,
        global_memory,
        fake_embedding.embed_sync([global_memory.content])[0],
    )
    _index(qdrant_index, expired, fake_embedding.embed_sync([expired.content])[0])
    graph_index.results = [target.id]
    return ids


@pytest.fixture()
def seeded_procedural(
    session_factory: sessionmaker[Session],
) -> dict[str, str]:
    """同一触发条件下，成功率不同的两条程序性记忆（global 作用域）。"""
    with session_factory() as session:
        if session.get(Workspace, WORKSPACE_ID) is None:
            session.add(Workspace(id=WORKSPACE_ID, name="测试工作区"))
            session.flush()
        high = _new_memory(
            session,
            content="高成功率：把 Excel 转成图表",
            importance=0.6,
            scope="global",
            scope_id=None,
            memory_type="procedural",
            metadata={
                "skill": {
                    "trigger": "用户要求把 Excel 转成图表",
                    "steps": ["读取列类型", "选择图表类型"],
                    "success_count": 9,
                    "failure_count": 0,
                }
            },
        )
        low = _new_memory(
            session,
            content="低成功率：把 Excel 转成图表",
            importance=0.6,
            scope="global",
            scope_id=None,
            memory_type="procedural",
            metadata={
                "skill": {
                    "trigger": "用户要求把 Excel 转成图表",
                    "steps": ["直接截图"],
                    "success_count": 0,
                    "failure_count": 5,
                }
            },
        )
        session.commit()
        return {"high": high.id, "low": low.id}


def test_search_fuses_vector_and_keyword(
    client, admin, seeded_memories
) -> None:
    r = client.post(
        "/api/v1/search",
        json={"query": "花生过敏", "workspace_id": "ws1", "end_user_id": "u1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"][0]["content"] == TARGET_CONTENT
    assert body["items"][0]["why"]["vector_rank"] is not None
    assert body["items"][0]["why"]["bm25_rank"] is not None
    assert body["context_text"]
    assert body["stats"]["returned"] == len(body["items"])


def test_search_degrades_when_vector_channel_unavailable(
    client, admin, seeded_memories, monkeypatch
) -> None:
    def raise_connection_error(self, *args, **kwargs):
        raise ConnectionError("qdrant unavailable")

    monkeypatch.setattr(QdrantIndex, "search", raise_connection_error)
    r = client.post(
        "/api/v1/search",
        json={"query": "花生过敏", "workspace_id": "ws1", "end_user_id": "u1"},
    )
    assert r.status_code == 200, r.text
    assert "vector" in r.json()["degraded"]
    assert r.json()["items"]


def test_search_never_leaks_other_scope(client, admin, seeded_memories) -> None:
    r = client.post(
        "/api/v1/search",
        json={"query": "花生过敏", "workspace_id": "ws1", "end_user_id": "u2"},
    )
    assert r.status_code == 200, r.text
    assert all(i["scope_id"] != "u1" for i in r.json()["items"])


def test_expired_fact_is_hidden_by_default_and_shown_with_history(
    client, admin, seeded_memories
) -> None:
    body = {"query": "用户住在哪里", "workspace_id": "ws1", "end_user_id": "u1"}
    default_items = client.post("/api/v1/search", json=body).json()["items"]
    assert "北京" not in [i["content"] for i in default_items]
    body["include_history"] = True
    history_items = client.post("/api/v1/search", json=body).json()["items"]
    assert "北京" in [i["content"] for i in history_items]


def test_search_rejects_empty_query(client, admin) -> None:
    r = client.post("/api/v1/search", json={"query": "  ", "workspace_id": "ws1"})
    assert r.status_code == 422


def test_procedural_memory_ranks_by_success_rate(
    client, admin, seeded_procedural
) -> None:
    items = client.post(
        "/api/v1/search",
        json={
            "query": "Excel 转图表",
            "workspace_id": "ws1",
            "types": ["procedural"],
        },
    ).json()["items"]
    assert items
    assert items[0]["content"].startswith("高成功率")


def test_deep_mode_includes_archived_and_survives_rerank_failure(
    client,
    admin,
    seeded_memories,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """deep 包含归档；重排异常只跳过重排，不影响主流程。"""
    with session_factory() as session:
        archived = session.get(Memory, seeded_memories["global"])
        assert archived is not None
        archived.status = "archived"
        session.commit()

    async def broken_rerank(self, query, documents):
        raise RuntimeError("reranker down")

    monkeypatch.setattr(NoopReranker, "rerank", broken_rerank)
    r = client.post(
        "/api/v1/search",
        json={
            "query": "工作区默认语言",
            "workspace_id": "ws1",
            "mode": "deep",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "reranker" not in body["degraded"]
    item = next(
        i for i in body["items"] if i["content"] == "工作区默认语言是中文"
    )
    assert item["historical"] is True
    assert item["status"] == "archived"
