"""任务 16：遗忘扫描、清理级联与索引对账。

覆盖简报中的生命周期用例；硬删除用例同时验证 Qdrant、Neo4j 与
``metadata.asset_paths`` 指向的本地文件都没有残留。
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory_core.decay import DecayParams

from app.adapters.embeddings import FakeEmbedding
from app.adapters.neo4j import Neo4jIndex
from app.adapters.qdrant import QdrantIndex
from app.jobs.runtime import Runtime, configure_runtime, reset_runtime
from app.jobs.tasks import dispatch_outbox
from app.models import AuditLog, Memory, OutboxEvent, ProjectionState, ReviewItem
from app.models.base import utcnow
from app.repositories.memory import MemoryRepository
from app.services.lifecycle import (
    decay_sweep,
    prune_ops_history,
    purge_deleted,
    reconcile_indexes,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 27, tzinfo=UTC)


class BrokenEmbedding:
    """模拟 Provider 不可用；对账不得写兜底向量。"""

    provider = "fake"
    name = "fake-8"
    identity = "fake:fake-8"
    dim = 8

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding provider unavailable")


def _memory(
    memory_id: str,
    *,
    workspace_id: str = "ws-lifecycle",
    status: str = "active",
    importance: float = 0.5,
    access_count: int = 0,
    stability: float = 1.0,
    last_reinforced_at: datetime | None = None,
    deleted_at: datetime | None = None,
    metadata: dict | None = None,
    content: str = "生命周期测试记忆",
) -> Memory:
    return Memory(
        id=memory_id,
        workspace_id=workspace_id,
        scope="user",
        scope_id="u1",
        type="semantic",
        content=content,
        importance=importance,
        access_count=access_count,
        stability=stability,
        status=status,
        last_reinforced_at=last_reinforced_at or NOW,
        valid_from=last_reinforced_at or NOW,
        deleted_at=deleted_at,
        metadata_=metadata or {},
    )


@pytest.fixture()
def old_memories(db_session: Session) -> list[Memory]:
    """低保留率与高保留率各一条；只有前者应被归档。"""
    low = _memory(
        "low",
        importance=0.1,
        access_count=0,
        last_reinforced_at=NOW - timedelta(days=30),
    )
    high = _memory(
        "high",
        importance=0.95,
        access_count=5,
        stability=1.5,
        last_reinforced_at=NOW - timedelta(days=30),
    )
    db_session.add_all([low, high])
    db_session.flush()
    return [low, high]


def test_decay_sweep_archives_only_low_retention(
    db_session: Session, old_memories: list[Memory]
) -> None:
    assert decay_sweep(db_session, NOW, DecayParams()) == 1
    assert db_session.get(Memory, "low").status == "archived"
    assert db_session.get(Memory, "high").status == "active"
    low = db_session.get(Memory, "low")
    assert low.metadata_["archived_reason"] == "low_retention"

    # 再次扫描不会重复归档，也不会触碰非 active 行。
    assert decay_sweep(db_session, NOW, DecayParams()) == 0


@pytest.fixture()
def deleted_memories(db_session: Session) -> list[Memory]:
    """30 天前软删的可清理；29 天前与仍在 active 的不可清理。"""
    old = _memory(
        "old", status="deleted", deleted_at=NOW - timedelta(days=31)
    )
    recent = _memory(
        "recent", status="deleted", deleted_at=NOW - timedelta(days=29)
    )
    active = _memory("active")
    db_session.add_all([old, recent, active])
    db_session.flush()
    return [old, recent, active]


def test_purge_removes_only_soft_deleted_older_than_30_days(
    db_session: Session, deleted_memories: list[Memory]
) -> None:
    assert purge_deleted(db_session, NOW) == 1
    assert db_session.get(Memory, "old").status == "purged"
    assert db_session.get(Memory, "recent").status == "deleted"
    assert db_session.get(Memory, "active").status == "active"

    # 重复执行幂等：已 purged 的行不会被再次处理。
    assert purge_deleted(db_session, NOW) == 0
    events = list(
        db_session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "memory.deleted")
        )
    )
    assert len(events) == 1
    assert events[0].payload["hard"] is True
    assert events[0].payload["memory_id"] == "old"


def test_purge_boundary_is_strictly_older_than_30_days(
    db_session: Session,
) -> None:
    exactly = _memory(
        "exactly-30d",
        status="deleted",
        deleted_at=NOW - timedelta(days=30),
    )
    older = _memory(
        "older-than-30d",
        status="deleted",
        deleted_at=NOW - timedelta(days=30, seconds=1),
    )
    db_session.add_all([exactly, older])
    db_session.flush()

    assert purge_deleted(db_session, NOW) == 1
    assert db_session.get(Memory, "exactly-30d").status == "deleted"
    assert db_session.get(Memory, "older-than-30d").status == "purged"


def test_prune_ops_history_respects_retention_windows(db_session: Session) -> None:
    old_audit = AuditLog(
        workspace_id="ws-lifecycle",
        actor_type="system",
        action="legacy.action",
        target_type="job",
        target_id="old",
    )
    new_audit = AuditLog(
        workspace_id="ws-lifecycle",
        actor_type="system",
        action="recent.action",
        target_type="job",
        target_id="new",
    )
    old_review = ReviewItem(
        workspace_id="ws-lifecycle",
        candidate={"content": "旧待审"},
        confidence=0.5,
    )
    new_review = ReviewItem(
        workspace_id="ws-lifecycle",
        candidate={"content": "新待审"},
        confidence=0.5,
    )
    db_session.add_all([old_audit, new_audit, old_review, new_review])
    db_session.flush()
    old_audit.created_at = NOW - timedelta(days=181)
    old_review.created_at = NOW - timedelta(days=91)
    db_session.flush()

    assert prune_ops_history(db_session, NOW) == 2
    remaining_audit = set(db_session.scalars(select(AuditLog.action)))
    remaining_review = {
        item.candidate["content"]
        for item in db_session.scalars(select(ReviewItem))
    }
    assert "legacy.action" not in remaining_audit
    assert "recent.action" in remaining_audit
    assert "旧待审" not in remaining_review
    assert "新待审" in remaining_review


@pytest.fixture()
def memory_with_attached_file(
    client,
    admin,
    workspace,
    session_factory,
    qdrant_index: QdrantIndex,
    neo4j_index: Neo4jIndex,
    tmp_path,
):
    """创建一条带本地资产的记忆，并写入两个真实索引。"""
    response = client.post(
        "/api/v1/memories",
        json={
            "workspace_id": workspace["id"],
            "content": "附件记忆将被硬删除",
            "type": "semantic",
            "scope": "user",
            "scope_id": "u-attach",
        },
    )
    assert response.status_code == 201, response.text
    memory_id = response.json()["id"]

    attached = tmp_path / "attached.txt"
    attached.write_bytes(b"attached asset")
    second = tmp_path / "second.txt"
    second.write_bytes(b"second asset")

    with session_factory() as session:
        memory = session.get(Memory, memory_id)
        memory.metadata_ = {"asset_paths": [str(attached), str(second)]}
        session.commit()

    with session_factory() as session:
        memory = session.get(Memory, memory_id)
        qdrant_index.upsert(
            memory_id,
            [0.1] * qdrant_index.dim,
            {
                "workspace_id": memory.workspace_id,
                "type": memory.type,
                "scope": memory.scope,
                "scope_id": memory.scope_id,
                "status": memory.status,
                "version": memory.version,
            },
        )
        neo4j_index.upsert_memory(memory)

    configure_runtime(
        Runtime(
            session_scope=session_factory,
            embedding=FakeEmbedding(dim=qdrant_index.dim),
            qdrant=qdrant_index,
            neo4j=neo4j_index,
            eager=True,
        )
    )
    try:
        yield SimpleNamespace(
            id=memory_id,
            workspace_id=workspace["id"],
            metadata_={"asset_paths": [str(attached), str(second)]},
        )
    finally:
        reset_runtime()


def test_hard_delete_cascades_to_indexes_and_attached_files(
    client,
    admin,
    memory_with_attached_file,
    qdrant_index: QdrantIndex,
    neo4j_index: Neo4jIndex,
) -> None:
    paths = [Path(p) for p in memory_with_attached_file.metadata_["asset_paths"]]
    response = client.delete(
        f"/api/v1/memories/{memory_with_attached_file.id}?hard=true&confirm=true"
    )
    assert response.status_code == 200, response.text
    assert qdrant_index.get(memory_with_attached_file.id) is None
    assert neo4j_index.get(memory_with_attached_file.id) is None
    assert all(not path.exists() for path in paths)


def test_soft_delete_keeps_assets_until_purge(
    client,
    admin,
    memory_with_attached_file,
    qdrant_index: QdrantIndex,
    neo4j_index: Neo4jIndex,
    run_worker_once,
    session_factory,
) -> None:
    paths = [Path(p) for p in memory_with_attached_file.metadata_["asset_paths"]]
    response = client.delete(f"/api/v1/memories/{memory_with_attached_file.id}")
    assert response.status_code == 204, response.text

    dispatch_outbox()
    run_worker_once()

    assert qdrant_index.get(memory_with_attached_file.id) is None
    assert neo4j_index.get(memory_with_attached_file.id) is None
    assert all(path.exists() for path in paths)
    with session_factory() as session:
        memory = session.get(Memory, memory_with_attached_file.id)
        assert memory.status == "deleted"
        assert memory.metadata_["asset_paths"] == [
            str(path) for path in paths
        ]


def test_hard_delete_is_idempotent_and_has_no_second_side_effect(
    client,
    admin,
    memory_with_attached_file,
    session_factory,
) -> None:
    paths = [Path(p) for p in memory_with_attached_file.metadata_["asset_paths"]]
    first = client.delete(
        f"/api/v1/memories/{memory_with_attached_file.id}?hard=true&confirm=true"
    )
    assert first.status_code == 200, first.text
    second = client.delete(
        f"/api/v1/memories/{memory_with_attached_file.id}?hard=true&confirm=true"
    )
    assert second.status_code == 404, second.text

    with session_factory() as session:
        memory = session.get(Memory, memory_with_attached_file.id)
        assert memory.status == "purged"
        assert memory.content == ""
        assert "source_quote" not in (memory.metadata_ or {})
        events = session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.aggregate_id == memory_with_attached_file.id,
                OutboxEvent.event_type == "memory.deleted",
            )
        )
        audits = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.target_id == memory_with_attached_file.id,
                AuditLog.action == "memory.deleted",
            )
        )
        assert events == 1
        assert audits == 1
    assert all(not path.exists() for path in paths)


def test_purge_worker_cascades_indexes_and_assets(
    memory_with_attached_file,
    qdrant_index: QdrantIndex,
    neo4j_index: Neo4jIndex,
    session_factory,
    run_worker_once,
) -> None:
    paths = [Path(p) for p in memory_with_attached_file.metadata_["asset_paths"]]
    with session_factory() as session:
        memory = session.get(Memory, memory_with_attached_file.id)
        MemoryRepository(session).update(
            memory,
            status="deleted",
            deleted_at=utcnow() - timedelta(days=31),
        )
        session.commit()

    with session_factory() as session:
        assert purge_deleted(session, utcnow()) == 1
        session.commit()

    dispatch_outbox()
    run_worker_once()

    assert qdrant_index.get(memory_with_attached_file.id) is None
    assert neo4j_index.get(memory_with_attached_file.id) is None
    assert all(not path.exists() for path in paths)


@pytest.fixture()
def memory(db_session: Session) -> Memory:
    row = _memory(
        str(uuid4()),
        workspace_id="ws-reconcile",
        content="用户喜欢喝绿茶",
    )
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture()
def reconcile_runtime(db_session: Session, qdrant_index: QdrantIndex):
    configure_runtime(
        Runtime(
            session_scope=lambda: nullcontext(db_session),
            embedding=FakeEmbedding(dim=qdrant_index.dim),
            qdrant=qdrant_index,
            neo4j=None,
            eager=True,
        )
    )
    try:
        yield
    finally:
        reset_runtime()


@pytest.fixture()
def broken_embedding_runtime(db_session: Session, qdrant_index: QdrantIndex):
    configure_runtime(
        Runtime(
            session_scope=lambda: nullcontext(db_session),
            embedding=BrokenEmbedding(),
            qdrant=qdrant_index,
            neo4j=None,
            eager=True,
        )
    )
    try:
        yield
    finally:
        reset_runtime()


def test_reconcile_reports_and_repairs_missing_points(
    db_session: Session,
    memory: Memory,
    qdrant_index: QdrantIndex,
    reconcile_runtime,
) -> None:
    qdrant_index.delete(memory.id)
    report = reconcile_indexes(db_session, qdrant_index, None, sample=10)
    assert report.repaired == 1 and report.missing_after == 0
    assert qdrant_index.get(memory.id) is not None
    assert report.checked >= 1


def test_reconcile_is_idempotent_after_repair(
    db_session: Session,
    memory: Memory,
    qdrant_index: QdrantIndex,
    reconcile_runtime,
) -> None:
    qdrant_index.delete(memory.id)
    assert reconcile_indexes(db_session, qdrant_index, None, sample=10).repaired == 1
    second = reconcile_indexes(db_session, qdrant_index, None, sample=10)
    assert second.repaired == 0
    assert second.missing_after == 0


def test_reconcile_does_not_write_fallback_vector_when_embedding_fails(
    db_session: Session,
    memory: Memory,
    qdrant_index: QdrantIndex,
    broken_embedding_runtime,
) -> None:
    qdrant_index.delete(memory.id)
    report = reconcile_indexes(db_session, qdrant_index, None, sample=10)

    assert report.repaired == 0
    assert report.failed >= 1
    assert report.missing_after >= 1
    assert qdrant_index.get(memory.id) is None
    assert (
        db_session.scalar(
            select(ProjectionState).where(
                ProjectionState.memory_id == memory.id
            )
        )
        is None
    )


def test_reconcile_cleans_purged_neo4j_and_assets_without_outbox(
    db_session: Session,
    qdrant_index: QdrantIndex,
    neo4j_index: Neo4jIndex,
    tmp_path,
) -> None:
    attached = tmp_path / "purged-asset.txt"
    attached.write_bytes(b"still here")
    purged = _memory(
        str(uuid4()),
        workspace_id="ws-purged",
        status="purged",
        deleted_at=NOW - timedelta(days=31),
        metadata={"asset_paths": [str(attached)]},
    )
    db_session.add(purged)
    db_session.flush()
    neo4j_index.upsert_memory(purged)
    assert neo4j_index.get(purged.id) is not None

    report = reconcile_indexes(
        db_session, qdrant_index, neo4j_index, sample=10
    )
    assert report.purged_checked == 1
    assert report.purged_cleaned == 1
    assert report.purged_failed == 0
    assert neo4j_index.get(purged.id) is None
    assert not attached.exists()

    # 再次对账没有可清理对象，也不会重复计失败。
    again = reconcile_indexes(
        db_session, qdrant_index, neo4j_index, sample=10
    )
    assert again.purged_cleaned == 0
    assert again.purged_failed == 0
