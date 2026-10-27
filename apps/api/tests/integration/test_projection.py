"""Outbox → 投影 worker → Qdrant 的端到端行为。

覆盖任务简报中的 4 条投影/分发用例，并补充版本幂等、删除投影与死信边界。
所有用例共享测试容器中的真实 Qdrant；Neo4j 由记录型替身承接，真实图投影
在 ``test_neo4j.py`` 中单独验证。
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from datetime import timedelta
from typing import Any

import pytest
from celery.exceptions import Retry
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.embeddings import FakeEmbedding
from app.adapters.qdrant import QdrantIndex
from app.jobs.runtime import Runtime, configure_runtime, reset_runtime
from app.jobs.tasks import (
    dispatch_outbox,
    heal_projection_identity,
    project_memory,
    project_qdrant,
    qdrant_target,
    reset_pending,
    run_worker_once,
)
from app.models import Memory, OutboxEvent, ProjectionState
from app.models.base import utcnow
from app.repositories.embedding import EmbeddingRepository
from app.repositories.memory import MemoryRepository
from app.repositories.outbox import OutboxRepository
from app.worker_cli import run_rebuild

pytestmark = pytest.mark.integration


class StubOpenAIEmbedding:
    """固定向量的 OpenAI 兼容替身，用于验证嵌入身份切换。"""

    provider = "openai"
    name = "text-embedding-3-small"
    identity = "openai:text-embedding-3-small"
    dim = 8

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * self.dim for _ in texts]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return self.embed_sync(texts)



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
    run_rebuild("qdrant", all_workspaces=True)
    assert qdrant_index.count("ws1") == 1


def test_rebuild_is_workspace_scoped(
    qdrant_index: QdrantIndex,
    db_session: Session,
    memory: Memory,
) -> None:
    other = Memory(
        workspace_id="ws2",
        scope="user",
        scope_id="u2",
        type="semantic",
        content="另一个工作区的内容",
        last_reinforced_at=utcnow(),
        valid_from=utcnow(),
    )
    db_session.add(other)
    db_session.flush()
    assert project_qdrant.run(memory.id, memory.version) is True
    assert project_qdrant.run(other.id, other.version) is True

    qdrant_index.delete(memory.id)
    run_rebuild("qdrant", workspace="ws1")

    assert qdrant_index.count("ws1") == 1
    assert qdrant_index.get(memory.id) is not None
    # 另一个 workspace 的 point 与 projection_state 都不能被删除。
    assert qdrant_index.get(other.id) is not None
    states = {
        state.memory_id
        for state in db_session.scalars(
            select(ProjectionState).where(
                ProjectionState.memory_id.in_([memory.id, other.id])
            )
        )
    }
    assert states == {memory.id, other.id}


def test_qdrant_delete_by_workspace_only_targets_one_workspace(
    qdrant_index: QdrantIndex,
    db_session: Session,
    memory: Memory,
) -> None:
    other = Memory(
        workspace_id="ws2",
        scope="user",
        scope_id="u2",
        type="semantic",
        content="另一个工作区的内容",
        last_reinforced_at=utcnow(),
        valid_from=utcnow(),
    )
    db_session.add(other)
    db_session.flush()
    assert project_qdrant.run(memory.id, memory.version) is True
    assert project_qdrant.run(other.id, other.version) is True

    qdrant_index.delete_by_workspace("ws1")
    assert qdrant_index.get(memory.id) is None
    assert qdrant_index.get(other.id) is not None


def test_outbox_dispatch_projects_memory(
    db_session: Session,
    memory_with_outbox: Memory,
    qdrant_index: QdrantIndex,
    fake_embedding: FakeEmbedding,
) -> None:
    dispatch_outbox()
    event = db_session.scalars(select(OutboxEvent)).one()
    db_session.refresh(event)
    assert event.status == "processing"
    assert event.status != "done"

    run_worker_once()
    db_session.refresh(event)
    assert event.status == "done"
    assert qdrant_index.get(memory_with_outbox.id) is not None


def test_projection_failure_records_error_and_keeps_event_processing(
    db_session: Session,
    memory_with_outbox: Memory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(QdrantIndex, "upsert", raise_connection_error)
    dispatch_outbox()
    with pytest.raises(Retry):
        run_worker_once()

    event = db_session.scalars(select(OutboxEvent)).one()
    db_session.refresh(event)
    assert event.status == "processing"
    assert "qdrant unavailable" in event.payload["last_error"]

    state = db_session.scalars(
        select(ProjectionState).where(
            ProjectionState.memory_id == memory_with_outbox.id
        )
    ).one()
    assert state.error is not None
    assert "qdrant unavailable" in state.error


def test_outbox_final_failure_marks_event_dead(
    db_session: Session,
    memory_with_outbox: Memory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(QdrantIndex, "upsert", raise_connection_error)
    dispatch_outbox()
    event = db_session.scalars(select(OutboxEvent)).one()
    try:
        project_memory.push_request(retries=8)
        with pytest.raises(Retry):
            project_memory.run(event.id, memory_with_outbox.id, memory_with_outbox.version)
    finally:
        project_memory.pop_request()
        reset_pending()

    db_session.refresh(event)
    assert event.status == "dead"
    assert "qdrant unavailable" in event.payload["last_error"]
    state = db_session.scalars(
        select(ProjectionState).where(
            ProjectionState.memory_id == memory_with_outbox.id
        )
    ).one()
    assert "qdrant unavailable" in state.error


def test_outbox_reembeds_after_content_change(
    db_session: Session,
    memory_with_outbox: Memory,
    fake_embedding: FakeEmbedding,
) -> None:
    dispatch_outbox()
    run_worker_once()
    first = EmbeddingRepository(db_session).get(
        memory_with_outbox.id, "fake:fake-8"
    )
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
    second = EmbeddingRepository(db_session).get(
        memory_with_outbox.id, "fake:fake-8"
    )
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
    event.status = "processing"
    event.next_attempt_at = utcnow() - timedelta(seconds=1)
    db_session.flush()

    assert dispatch_outbox() == 0
    assert event.status == "dead"


def test_rebuild_reembeds_with_new_provider_identity(
    db_session: Session,
    memory: Memory,
    qdrant_url: str,
) -> None:
    assert project_qdrant.run(memory.id, memory.version) is True
    fake_identity = "fake:fake-8"
    fake_vector = EmbeddingRepository(db_session).get(memory.id, fake_identity)
    assert fake_vector is not None

    openai_index = QdrantIndex(
        qdrant_url,
        provider="openai",
        model="text-embedding-3-small",
        dim=8,
    )
    try:
        configure_runtime(
            Runtime(
                session_scope=lambda: nullcontext(db_session),
                embedding=StubOpenAIEmbedding(),
                qdrant=openai_index,
                neo4j=RecordingGraphIndex(),
                eager=True,
            )
        )
        run_rebuild("qdrant", workspace="ws1")

        assert openai_index.count("ws1") == 1
        openai_vector = EmbeddingRepository(db_session).get(
            memory.id, "openai:text-embedding-3-small"
        )
        assert openai_vector == [1.0] * 8
        # 切换 provider 后不会复用旧的 fake 向量。
        assert openai_vector != fake_vector
    finally:
        openai_index.delete_collection()
        openai_index.close()


def test_projection_backfills_new_collection(
    db_session: Session,
    memory: Memory,
    qdrant_url: str,
) -> None:
    assert project_qdrant.run(memory.id, memory.version) is True
    alt_index = QdrantIndex(
        qdrant_url, provider="fake", model="fake-alt-8", dim=8
    )
    try:
        configure_runtime(
            Runtime(
                session_scope=lambda: nullcontext(db_session),
                embedding=FakeEmbedding(dim=8, model="fake-alt-8"),
                qdrant=alt_index,
                neo4j=RecordingGraphIndex(),
                eager=True,
            )
        )
        # 版本未变，但 collection 身份变了，必须回填新 collection。
        assert project_qdrant.run(memory.id, memory.version) is True
        assert alt_index.count("ws1") == 1
        targets = set(
            db_session.scalars(
                select(ProjectionState.target).where(
                    ProjectionState.memory_id == memory.id
                )
            )
        )
        assert targets == {
            "qdrant:mem_fake_fake-8_8",
            qdrant_target(alt_index),
        }
    finally:
        alt_index.delete_collection()
        alt_index.close()


def test_heal_projection_identity_backfills_new_collection(
    db_session: Session,
    memory: Memory,
    qdrant_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert project_qdrant.run(memory.id, memory.version) is True
    old_target = "qdrant:mem_fake_fake-8_8"
    alt_index = QdrantIndex(
        qdrant_url, provider="fake", model="fake-alt-8", dim=8
    )
    try:
        configure_runtime(
            Runtime(
                session_scope=lambda: nullcontext(db_session),
                embedding=FakeEmbedding(dim=8, model="fake-alt-8"),
                qdrant=alt_index,
                neo4j=RecordingGraphIndex(),
                eager=True,
            )
        )
        with caplog.at_level(logging.WARNING, logger="app.jobs.tasks"):
            healed = heal_projection_identity.run(limit=10)
            assert healed == 1
            run_worker_once()

        assert alt_index.count("ws1") == 1
        state = db_session.scalars(
            select(ProjectionState).where(
                ProjectionState.memory_id == memory.id,
                ProjectionState.target == qdrant_target(alt_index),
            )
        ).one()
        assert state.version == memory.version
        # 旧 target 的状态保留，用于回滚/审计。
        old_state = db_session.scalars(
            select(ProjectionState).where(
                ProjectionState.memory_id == memory.id,
                ProjectionState.target == old_target,
            )
        ).one()
        assert old_state.version == memory.version
        assert "projection identity drift" in caplog.text
    finally:
        alt_index.delete_collection()
        alt_index.close()
