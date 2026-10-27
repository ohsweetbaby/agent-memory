"""待审队列 REST：批准、拒绝、批量与 workspace 隔离。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Memory, ReviewItem
from app.models.base import utcnow
from app.services.review import purge_expired_review_items

pytestmark = pytest.mark.integration


def _create_item(
    session_factory,
    workspace_id: str,
    content: str,
    *,
    importance: float = 0.6,
    status: str = "pending",
) -> str:
    with session_factory() as session:
        item = ReviewItem(
            workspace_id=workspace_id,
            candidate={
                "content": content,
                "type": "semantic",
                "importance": importance,
                "confidence": 0.5,
                "entities": [],
                "scope": "user",
                "scope_id": "u-review",
                "session_id": "s-review",
            },
            evidence={"quote": content},
            confidence=0.5,
            status=status,
        )
        session.add(item)
        session.commit()
        return item.id


def _actions(session_factory) -> set[str]:
    with session_factory() as session:
        return {row.action for row in session.scalars(select(AuditLog))}


def test_approve_creates_memory_and_marks_reviewed(
    client, admin, pending_review, session_factory
) -> None:
    r = client.post(
        f"/api/v1/review/{pending_review.id}/approve",
        json={"content": "用户对花生过敏"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["memory_id"]
    assert client.get("/api/v1/review").json()["items"] == []

    with session_factory() as session:
        memory = session.get(Memory, r.json()["memory_id"])
        assert memory is not None
        assert memory.content == "用户对花生过敏"
        item = session.get(ReviewItem, pending_review.id)
        assert item is not None
        assert item.status == "approved"
        assert item.reviewed_at is not None
    assert "review.approved" in _actions(session_factory)


def test_approve_without_edits_uses_candidate_content(
    client, admin, pending_review, session_factory
) -> None:
    r = client.post(f"/api/v1/review/{pending_review.id}/approve", json={})

    assert r.status_code == 200, r.text
    with session_factory() as session:
        memory = session.get(Memory, r.json()["memory_id"])
        assert memory is not None
        assert memory.content == "用户可能对花生过敏"
        assert memory.importance == pytest.approx(0.6)


def test_reject_marks_reviewed_and_audits(
    client, admin, pending_review, session_factory
) -> None:
    r = client.post(
        f"/api/v1/review/{pending_review.id}/reject",
        json={"reason": "证据不足"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    body = client.get("/api/v1/review").json()
    assert body["items"] == []

    with session_factory() as session:
        item = session.get(ReviewItem, pending_review.id)
        assert item is not None
        assert item.status == "rejected"
        assert item.reviewed_at is not None
        assert session.query(Memory).count() == 0
    assert "review.rejected" in _actions(session_factory)


def test_bulk_approve_and_reject(
    client, admin, workspace, session_factory
) -> None:
    first = _create_item(session_factory, workspace["id"], "用户喜欢绿茶")
    second = _create_item(session_factory, workspace["id"], "用户讨厌咖啡")
    third = _create_item(session_factory, workspace["id"], "用户可能住在杭州")

    approved = client.post(
        "/api/v1/review/bulk",
        json={"ids": [first, second], "action": "approve"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved"] == 2
    assert len(approved.json()["memory_ids"]) == 2

    rejected = client.post(
        "/api/v1/review/bulk",
        json={"ids": [third], "action": "reject", "reason": "证据不足"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["rejected"] == 1

    assert client.get("/api/v1/review").json()["items"] == []
    with session_factory() as session:
        assert session.query(Memory).count() == 2
        assert {item.status for item in session.scalars(select(ReviewItem))} == {
            "approved",
            "rejected",
        }


def test_bulk_rejects_invalid_action(client, admin, workspace, session_factory) -> None:
    item_id = _create_item(session_factory, workspace["id"], "用户喜欢绿茶")
    r = client.post(
        "/api/v1/review/bulk", json={"ids": [item_id], "action": "delete"}
    )
    assert r.status_code == 422


def test_review_item_is_scoped_to_api_key_workspace(
    client, admin, workspace, write_key, session_factory
) -> None:
    other = client.post("/api/v1/workspaces", json={"name": "另一个工作区"}).json()
    foreign = _create_item(session_factory, other["id"], "其他工作区的候选")

    client.headers["Authorization"] = f"Bearer {write_key}"

    listed = client.get("/api/v1/review")
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []

    forbidden = client.post(f"/api/v1/review/{foreign}/approve", json={})
    assert forbidden.status_code == 404
    assert forbidden.json()["error"]["code"] == "not_found"


def test_purge_removes_items_older_than_retention(
    db_session: Session, session_factory, workspace
) -> None:
    item_id = _create_item(session_factory, workspace["id"], "过期候选")
    with session_factory() as session:
        item = session.get(ReviewItem, item_id)
        assert item is not None
        item.created_at = utcnow() - timedelta(days=91)
        session.commit()

    removed = purge_expired_review_items(db_session, retention_days=90)

    assert removed == 1
    assert db_session.get(ReviewItem, item_id) is None


def test_expired_cleanup_keeps_recent_items(
    db_session: Session, session_factory, workspace
) -> None:
    fresh = _create_item(session_factory, workspace["id"], "新鲜候选")

    removed = purge_expired_review_items(db_session, retention_days=90)

    assert removed == 0
    assert db_session.get(ReviewItem, fresh) is not None
