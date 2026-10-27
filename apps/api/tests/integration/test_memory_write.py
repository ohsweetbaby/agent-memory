"""记忆写入主链路：事务、幂等、冲突与实体关联。"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Entity, Memory, MemoryEntity, OutboxEvent
from app.schemas.memory import MemoryWriteRequest
from app.services.memory_write import get_conflict_judge, write_memory

pytestmark = pytest.mark.integration

WORKERS = 2


class FakeContradictionJudge:
    """把“北京 → 上海”固定判为语义矛盾，避免测试依赖真实 LLM。"""

    def is_contradiction(self, new_content: str, candidate_content: str) -> bool | None:
        return "北京" in candidate_content and "上海" in new_content


@pytest.fixture()
def fake_judge(app):
    app.dependency_overrides[get_conflict_judge] = lambda: FakeContradictionJudge()
    return FakeContradictionJudge()


def _run_concurrently(
    session_factory, workers: int, call: Callable[[Session], Any]
) -> list[Any]:
    barrier = threading.Barrier(workers)

    def attempt(_: int) -> Any:
        session = session_factory()
        try:
            barrier.wait(timeout=30)
            return call(session)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(attempt, range(workers)))


def test_write_creates_memory_and_outbox_row(
    client, admin, db_session, valid_write_payload
) -> None:
    r = client.post(
        "/api/v1/memories", json=valid_write_payload(content="用户对花生过敏")
    )
    assert r.status_code == 201, r.text
    assert r.json()["action"] == "created"
    events = (
        db_session.query(OutboxEvent)
        .filter_by(aggregate_id=r.json()["id"])
        .all()
    )
    assert len(events) == 1
    assert events[0].event_type == "memory.upserted"
    assert events[0].payload["memory_id"] == r.json()["id"]
    assert events[0].payload["version"] >= 1


def test_blank_content_returns_422(client, admin, valid_write_payload) -> None:
    r = client.post("/api/v1/memories", json=valid_write_payload(content="   \n  "))
    assert r.status_code == 422
    assert r.json()["error"]["details"]["field"] == "content"


def test_rejects_content_over_8kb(client, admin, valid_write_payload) -> None:
    r = client.post(
        "/api/v1/memories", json=valid_write_payload(content="字" * 8193)
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "content_too_long"


def test_concurrent_same_idempotency_key_creates_one_memory(
    session_factory, admin, valid_write_payload
) -> None:
    req = MemoryWriteRequest(**valid_write_payload(idempotency_key="idem-1"))

    ids = _run_concurrently(
        session_factory,
        WORKERS,
        lambda session: write_memory(session, req, admin).id,
    )

    assert ids[0] == ids[1]
    with session_factory() as s:
        assert s.query(Memory).count() == 1


def test_contradiction_supersedes_and_keeps_history(
    client, admin, valid_write_payload, fake_judge
) -> None:
    first = client.post(
        "/api/v1/memories", json=valid_write_payload(content="用户住在北京")
    ).json()
    second = client.post(
        "/api/v1/memories", json=valid_write_payload(content="用户已搬到上海")
    ).json()
    assert second["action"] == "superseded"
    old = client.get(f"/api/v1/memories/{first['id']}").json()
    assert old["status"] == "superseded" and old["superseded_by"] == second["id"]


def test_write_persists_entities_and_links(
    client, admin, db_session, valid_write_payload
) -> None:
    payload = valid_write_payload(
        content="用户使用 Python",
        entities=[{"name": "Python", "type": "technology"}],
    )
    response = client.post("/api/v1/memories", json=payload)
    assert response.status_code == 201, response.text
    assert db_session.query(Entity).filter_by(name="Python").count() == 1
    assert db_session.query(MemoryEntity).count() == 1


def test_near_duplicate_merges_and_accumulates_evidence(
    client, admin, db_session, valid_write_payload
) -> None:
    first = client.post(
        "/api/v1/memories",
        json=valid_write_payload(content="the user is allergic to peanuts"),
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/v1/memories",
        json=valid_write_payload(content="the user is allergic to peanuts"),
    )
    assert second.status_code == 201, second.text
    assert second.json()["action"] == "merged"
    assert second.json()["id"] == first.json()["id"]

    assert db_session.query(Memory).count() == 1
    detail = client.get(f"/api/v1/memories/{first.json()['id']}").json()
    assert detail["access_count"] == 1
    assert detail["version"] >= 2
    assert detail["metadata"]["merge_evidence"]


def test_idempotency_key_is_kept_when_write_merges(
    client, admin, valid_write_payload
) -> None:
    first = client.post(
        "/api/v1/memories",
        json=valid_write_payload(content="the user prefers dark mode"),
    ).json()
    payload = valid_write_payload(
        content="the user prefers dark mode", idempotency_key="merge-idem"
    )
    merged = client.post("/api/v1/memories", json=payload)
    assert merged.status_code == 201, merged.text
    assert merged.json()["action"] == "merged"
    replay = client.post("/api/v1/memories", json=payload)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first["id"]
    detail = client.get(f"/api/v1/memories/{first['id']}").json()
    assert detail["access_count"] == 1


def test_rule_judge_supersedes_location_change_without_llm(
    client, admin, valid_write_payload
) -> None:
    first = client.post(
        "/api/v1/memories", json=valid_write_payload(content="用户住在杭州")
    ).json()
    second = client.post(
        "/api/v1/memories", json=valid_write_payload(content="用户已搬到南京")
    ).json()
    assert second["action"] == "superseded"
    old = client.get(f"/api/v1/memories/{first['id']}").json()
    assert old["status"] == "superseded" and old["superseded_by"] == second["id"]


def test_encoding_marks_pii_and_honors_explicit_remember(
    client, admin, valid_write_payload
) -> None:
    quote = "来源原话" * 100
    response = client.post(
        "/api/v1/memories",
        json=valid_write_payload(
            content="请记住：联系 test@example.com",
            source={"type": "message", "id": "msg-1", "quote": quote},
            ttl_seconds=3600,
        ),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["importance"] >= 0.9
    assert body["metadata"]["importance_reason"] == "explicit_remember"
    assert body["metadata"]["pii"] is True
    # PII 只标记，不改写原文。
    assert "test@example.com" in body["content"]
    assert len(body["metadata"]["source_quote"]) == 200
    assert body["valid_to"] is not None
    assert body["source_type"] == "message"
    assert body["source_id"] == "msg-1"


def test_nul_character_returns_422(client, admin, valid_write_payload) -> None:
    r = client.post(
        "/api/v1/memories", json=valid_write_payload(content="用户\x00过敏")
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_character"
    details = r.json()["error"]["details"]
    assert details["field"] == "content"
    assert details["character"] == "\\u0000"


def test_lone_surrogate_returns_422(client, admin, valid_write_payload) -> None:
    # httpx 的 json= 会用 UTF-8 直接编码，无法发送孤立代理对；这里手工构造
    # JSON 转义，模拟客户端确实传入了 \ud800。
    payload = valid_write_payload(content="placeholder")
    payload["content"] = "用户\ud800"
    r = client.post(
        "/api/v1/memories",
        content=json.dumps(payload, ensure_ascii=True),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_character"
    assert r.json()["error"]["details"]["character"] == "\\ud800"


def test_other_control_characters_roundtrip(
    client, admin, valid_write_payload
) -> None:
    content = "用户说\x07\x1b[31m红色\x1b[0m"
    r = client.post("/api/v1/memories", json=valid_write_payload(content=content))
    assert r.status_code == 201, r.text
    detail = client.get(f"/api/v1/memories/{r.json()['id']}").json()
    assert detail["content"] == content


def test_temporal_change_coexists_and_closes_old_validity(
    client, admin, db_session, valid_write_payload
) -> None:
    first = client.post(
        "/api/v1/memories", json=valid_write_payload(content="用户住在北京")
    ).json()
    second = client.post(
        "/api/v1/memories",
        json=valid_write_payload(content="用户搬到上海", temporal_change=True),
    )
    assert second.status_code == 201, second.text
    assert second.json()["action"] == "coexist"

    old = client.get(f"/api/v1/memories/{first['id']}").json()
    assert old["status"] == "active"
    assert old["valid_to"] is not None
    assert old["superseded_by"] is None

    new = client.get(f"/api/v1/memories/{second.json()['id']}").json()
    assert new["status"] == "active"
    assert new["superseded_by"] is None

    assert db_session.scalar(
        select(AuditLog.id).where(
            AuditLog.action == "memory.updated",
            AuditLog.target_id == first["id"],
        )
    )
