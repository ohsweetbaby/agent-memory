"""MemoryRepository / OutboxRepository 的数据库行为。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Memory
from app.repositories.memory import MemoryFilters, MemoryRepository
from app.repositories.outbox import OutboxRepository

pytestmark = pytest.mark.integration


def make_memory(**overrides: Any) -> Memory:
    values: dict[str, Any] = {
        "workspace_id": "ws1",
        "scope": "global",
        "scope_id": None,
        "type": "semantic",
        "content": "默认内容",
    }
    values.update(overrides)
    return Memory(**values)


def test_memory_roundtrip_is_workspace_scoped(db_session: Session) -> None:
    repo = MemoryRepository(db_session)
    memory = repo.add(make_memory(workspace_id="ws1", content="用户对花生过敏"))
    assert memory.id
    assert repo.get("ws1", memory.id).content == "用户对花生过敏"
    assert repo.get("ws2", memory.id) is None


def test_similarity_search_is_scoped_by_scope(db_session: Session) -> None:
    repo = MemoryRepository(db_session)
    repo.add(make_memory(workspace_id="ws1", scope="user", scope_id="u1", content="用户住在北京"))
    repo.add(make_memory(workspace_id="ws1", scope="user", scope_id="u2", content="用户住在北京"))
    hits = repo.find_similar("ws1", [("user", "u1")], "用户住在北京市")
    assert hits, "同 scope 下应至少命中一条记忆"
    assert all(memory.scope_id == "u1" for memory, _ in hits)
    assert all(score > 0 for _, score in hits)


def test_update_bumps_version_and_refreshes_timestamp(db_session: Session) -> None:
    repo = MemoryRepository(db_session)
    memory = repo.add(make_memory(content="初始内容"))
    assert memory.version == 1
    original_updated_at = memory.updated_at

    repo.update(memory, content="更新后的内容", importance=0.9)
    db_session.flush()
    assert memory.version == 2
    assert memory.content == "更新后的内容"
    assert memory.importance == 0.9
    assert memory.updated_at >= original_updated_at

    # 绕过仓储直接改 ORM 对象也必须自增，且内存值与数据库一致。
    memory.content = "直接修改"
    db_session.flush()
    assert memory.version == 3


def test_outbox_claim_batch_marks_events_processing(db_session: Session) -> None:
    repo = OutboxRepository(db_session)
    event = repo.add("memory.created", "m-1", {"memory_id": "m-1"})
    assert event.status == "pending"

    claimed = repo.claim_batch(limit=10)
    assert [item.id for item in claimed] == [event.id]
    assert claimed[0].status == "processing"
    assert claimed[0].attempts == 1
    assert repo.claim_batch(limit=10) == []


def test_filters_narrow_listing(db_session: Session) -> None:
    repo = MemoryRepository(db_session)
    repo.add(make_memory(scope="user", scope_id="u1", content="北京"))
    repo.add(make_memory(scope="agent", scope_id="a1", content="上海"))

    rows = repo.list("ws1", MemoryFilters(scope="user", scope_id="u1"))
    assert [memory.content for memory in rows] == ["北京"]


def test_search_tsv_and_version_triggers(db_session: Session) -> None:
    memory = MemoryRepository(db_session).add(make_memory(content="用户对花生过敏"))

    tsv = db_session.execute(
        text("SELECT search_tsv::text FROM memory WHERE id = :id"),
        {"id": memory.id},
    ).scalar_one()
    assert tsv

    # 绕过 ORM 的 UPDATE 也必须抬升 version。
    db_session.execute(
        text("UPDATE memory SET content = :content WHERE id = :id"),
        {"content": "更新内容", "id": memory.id},
    )
    db_session.expire(memory)
    assert memory.version == 2

