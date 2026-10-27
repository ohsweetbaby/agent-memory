"""Outbox → 投影 worker → Qdrant 的端到端行为。

覆盖任务简报中的 4 条投影/分发用例，并补充版本幂等、删除投影与死信边界。
所有用例共享测试容器中的真实 Qdrant；Neo4j 由记录型替身承接，真实图投影
在 ``test_neo4j.py`` 中单独验证。
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import timedelta
from typing import Any

import pytest
from celery.exceptions import Retry
from sqlalchemy.orm import Session

from app.adapters.embeddings import FakeEmbedding
from app.adapters.qdrant import QdrantIndex
from app.jobs.runtime import Runtime, configure_runtime, reset_runtime
from app.jobs.tasks import dispatch_outbox, project_qdrant, run_worker_once
from app.models import Memory
from app.models.base import utcnow
from app.repositories.embedding import EmbeddingRepository
from app.repositories.memory import MemoryRepository
from app.repositories.outbox import OutboxRepository
from app.worker_cli import run_rebuild

pytestmark = pytest.mark.integration


class RecordingGraphIndex:
    """Neo4jIndex 的最小替身，避免每条投影用例都启动图库容器。"""

    def __init__(self) -> None:
        self.memories: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []

    def upsert_memory(self, memory: Memory, entities=(), links=()) -> None:
        self.memories[memory.id] = {
            "id": memory.id,
            "workspace_id": memory.workspace_id,
            "entity_ids": [entity.id for entity in entities],
        }

    def get(self, memory_id: str) -> dict[str, Any] | None:
        return self.memories.get(memory_id)

    def delete(self, memory_id: str) -> None:
        self.deleted.append(memory_id)
        self.memories.pop(memory_id, None)

    def expand(self, entity_ids, hops: int = 2, limit: int = 30) -> list[str]:
        return []


@pytest.fixture()
def fake_embedding() -> FakeEmbedding:
    return FakeEmbedding(dim=8)


@pytest.fixture()
def memory(db_session: Session) -> Memory:
    """直接落库的测试记忆；brief 用例统一使用 ws1。"""
    row = Memory(
        workspace_id="ws1",
        scope="user",
        scope_id="u1",
        type="semantic",
        content="用户喜欢喝绿茶",
        importance=0.7,
        status="active",
        last_reinforced_at=utcnow(),
        valid_from=utcnow(),
    )
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture()
def memory_with_outbox(db_session: Session, memory: Memory) -> Memory:
    OutboxRepository(db_session).add(
        "memory.upserted",
        memory.id,
        {
            "memory_id": memory.id,
            "version": memory.version,
            "workspace_id": memory.workspace_id,
            "action": "created",
            "status": memory.status,
            "type": memory.type,
            "scope": memory.scope,
            "scope_id": memory.scope_id,
        },
    )
    db_session.flush()
    return memory


@pytest.fixture(autouse=True)
def projection_runtime(
    db_session: Session,
    fake_embedding: FakeEmbedding,
    qdrant_index: QdrantIndex,
) -> RecordingGraphIndex:
    """把任务运行时指向当前测试事务与真实 Qdrant，并用内存队列模拟 broker。"""
    graph = RecordingGraphIndex()
    configure_runtime(
        Runtime(
            session_scope=lambda: nullcontext(db_session),
            embedding=fake_embedding,
            qdrant=qdrant_index,
            neo4j=graph,
            eager=True,
        )
    )
    try:
        yield graph
    finally:
        reset_runtime()


def raise_connection_error(*args: Any, **kwargs: Any) -> None:
    raise ConnectionError("qdrant unavailable")


def test_projection_writes_qdrant_point(
    fake_embedding: FakeEmbedding,
    qdrant_index: QdrantIndex,
    db_session: Session,
    memory: Memory,
) -> None:
    project_qdrant.run(memory.id, memory.version)
    hit = qdrant_index.search([1.0] * 8, {"workspace_id": "ws1"}, limit=1)[0]
    assert hit.id == memory.id
    assert hit.payload["workspace_id"] == "ws1"
    assert hit.payload["scope"] == "user"
    assert hit.payload["scope_id"] == "u1"
    assert hit.payload["type"] == "semantic"
    assert hit.payload["status"] == "active"
    assert hit.payload["importance"] == pytest.approx(0.7)


def test_projection_retries_when_qdrant_down(
    db_session: Session,
    memory: Memory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(QdrantIndex, "upsert", raise_connection_error)
    with pytest.raises(Retry):
        project_qdrant.run(memory.id, memory.version)
    assert MemoryRepository(db_session).get("ws1", memory.id).status == "active"


def test_rebuild_recreates_missing_points(
    qdrant_index: QdrantIndex,
    db_session: Session,
    memory: Memory,
) -> None:
    qdrant_index.delete(memory.id)
    run_rebuild("qdrant")
    assert qdrant_index.count("ws1") == 1


def test_outbox_dispatch_projects_memory(
    db_session: Session,
    memory_with_outbox: Memory,
    qdrant_index: QdrantIndex,
    fake_embedding: FakeEmbedding,
) -> None:
    dispatch_outbox()
    run_worker_once()
    assert qdrant_index.get(memory_with_outbox.id) is not None


def test_outbox_reembeds_after_content_change(
    db_session: Session,
    memory_with_outbox: Memory,
    fake_embedding: FakeEmbedding,
) -> None:
    dispatch_outbox()
    run_worker_once()
    first = EmbeddingRepository(db_session).get(memory_with_outbox.id, "fake-8")
    assert first == fake_embedding.embed_sync([memory_with_outbox.content])[0]

    memory_with_outbox.content = "用户改喝红茶"
    db_session.flush()
    OutboxRepository(db_session).add(
        "memory.upserted",
        memory_with_outbox.id,
        {
            "memory_id": memory_with_outbox.id,
            "version": memory_with_outbox.version,
            "workspace_id": "ws1",
        },
    )
    db_session.flush()

    dispatch_outbox()
    run_worker_once()
    second = EmbeddingRepository(db_session).get(memory_with_outbox.id, "fake-8")
    assert second == fake_embedding.embed_sync(["用户改喝红茶"])[0]
    assert second != first


def test_projection_is_idempotent_by_version(
    db_session: Session,
    memory: Memory,
    qdrant_index: QdrantIndex,
) -> None:
    assert project_qdrant.run(memory.id, memory.version) is True
    assert project_qdrant.run(memory.id, memory.version) is False
    assert qdrant_index.count("ws1") == 1

    # 过期版本不能覆盖已投影状态。
    assert project_qdrant.run(memory.id, memory.version - 1) is False


def test_deleted_memory_is_removed_from_index(
    db_session: Session,
    memory: Memory,
    qdrant_index: QdrantIndex,
) -> None:
    project_qdrant.run(memory.id, memory.version)
    assert qdrant_index.get(memory.id) is not None

    memory.status = "deleted"
    db_session.flush()
    assert project_qdrant.run(memory.id, memory.version) is True
    assert qdrant_index.get(memory.id) is None


def test_outbox_dispatch_dead_letters_exhausted_events(
    db_session: Session,
    memory: Memory,
) -> None:
    event = OutboxRepository(db_session).add(
        "memory.upserted",
        memory.id,
        {"memory_id": memory.id, "version": memory.version, "workspace_id": "ws1"},
    )
    event.attempts = 8
    event.next_attempt_at = utcnow() - timedelta(seconds=1)
    db_session.flush()

    assert dispatch_outbox() == 0
    assert event.status == "dead"
