"""对话抽取：自动入库阈值、待审分流、幂等与失败降级。"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.embeddings import FakeEmbedding
from app.jobs.runtime import Runtime, configure_runtime, reset_runtime
from app.jobs.tasks import reset_pending
from app.models import AuditLog, JobRun, Memory, ReviewItem
from app.models.base import utcnow
from app.services.extraction import ingest_conversation

pytestmark = pytest.mark.integration


class NullIndex:
    """抽取测试不会触发投影；占位以满足 Runtime 依赖。"""

    def upsert(self, *args: Any, **kwargs: Any) -> None:
        return None

    def delete(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def extraction_runtime(db_session: Session, fake_llm) -> Any:
    """worker 直接复用测试事务与会话，LLM 走 FakeLLM。"""
    reset_pending()
    configure_runtime(
        Runtime(
            session_scope=lambda: nullcontext(db_session),
            embedding=FakeEmbedding(dim=8),
            qdrant=NullIndex(),
            neo4j=None,
            eager=True,
            llm=fake_llm,
        )
    )
    try:
        yield
    finally:
        reset_pending()
        reset_runtime()


def _candidate_json(content: str = "用户对花生过敏", **overrides: Any) -> list[dict[str, Any]]:
    item: dict[str, Any] = {
        "content": content,
        "type": "semantic",
        "importance": 0.9,
        "confidence": 0.9,
        "evidence": "我吃花生会过敏",
    }
    item.update(overrides)
    return [item]


def _job(db_session: Session, task_id: str) -> JobRun:
    job = db_session.get(JobRun, task_id)
    assert job is not None
    return job


def test_high_confidence_candidate_is_auto_accepted(
    db_session: Session, fake_llm, admin, ingest_payload, run_worker_once
) -> None:
    fake_llm.set_json(_candidate_json())

    ingest_conversation(db_session, ingest_payload("我吃花生会过敏"), admin)
    run_worker_once()

    assert db_session.query(Memory).filter_by(content="用户对花生过敏").count() == 1
    assert db_session.query(ReviewItem).count() == 0
    assert fake_llm.calls == 1


def test_low_confidence_goes_to_review(
    db_session: Session, fake_llm, admin, ingest_payload, run_worker_once
) -> None:
    fake_llm.set_json(
        _candidate_json(
            "用户可能喜欢猫", importance=0.4, confidence=0.5, evidence="也许吧"
        )
    )

    ingest_conversation(db_session, ingest_payload("也许吧"), admin)
    run_worker_once()

    items = list(db_session.scalars(select(ReviewItem)))
    assert len(items) == 1
    assert items[0].status == "pending"
    assert items[0].candidate["content"] == "用户可能喜欢猫"
    assert items[0].evidence["quote"] == "也许吧"
    assert db_session.query(Memory).count() == 0


def test_ingest_is_idempotent_for_same_messages(
    db_session: Session, fake_llm, admin, ingest_payload, run_worker_once
) -> None:
    payload = ingest_payload("我吃花生会过敏")
    fake_llm.set_json(_candidate_json())

    a = ingest_conversation(db_session, payload, admin)
    b = ingest_conversation(db_session, payload, admin)

    assert a.task_id == b.task_id
    run_worker_once()
    assert fake_llm.calls == 1
    assert db_session.query(JobRun).count() == 1
    assert _job(db_session, a.task_id).type == "extract_conversation"
    assert db_session.query(Memory).filter_by(content="用户对花生过敏").count() == 1


def test_invalid_llm_output_is_dead_letter_and_audited(
    db_session: Session, fake_llm, admin, ingest_payload, run_worker_once
) -> None:
    fake_llm.set_text("抱歉，我无法输出 JSON。")

    result = ingest_conversation(db_session, ingest_payload("我吃花生会过敏"), admin)
    run_worker_once()

    job = _job(db_session, result.task_id)
    assert job.status == "dead"
    assert "JSON" in (job.error or "")
    assert db_session.query(Memory).count() == 0
    assert db_session.query(ReviewItem).count() == 0
    actions = {row.action for row in db_session.scalars(select(AuditLog))}
    assert "ingest.created" in actions
    assert "ingest.failed" in actions


def test_candidate_with_nul_is_rejected_without_persisting(
    db_session: Session, fake_llm, admin, ingest_payload, run_worker_once
) -> None:
    fake_llm.set_json(_candidate_json("用户对\u0000花生过敏"))

    result = ingest_conversation(db_session, ingest_payload("我吃花生会过敏"), admin)
    run_worker_once()

    job = _job(db_session, result.task_id)
    assert job.status == "completed"
    assert job.result["skipped"] == 1
    assert db_session.query(Memory).count() == 0
    assert db_session.query(ReviewItem).count() == 0


def test_duplicate_candidate_is_deduped_against_existing_memory(
    db_session: Session, fake_llm, admin, ingest_payload, run_worker_once
) -> None:
    payload = ingest_payload("我吃花生会过敏")
    db_session.add(
        Memory(
            workspace_id=payload.workspace_id,
            scope="session",
            scope_id=payload.session_id,
            type="semantic",
            content="用户对花生过敏",
            importance=0.8,
            status="active",
            last_reinforced_at=utcnow(),
            valid_from=utcnow(),
        )
    )
    db_session.flush()
    fake_llm.set_json(_candidate_json())

    result = ingest_conversation(db_session, payload, admin)
    run_worker_once()

    job = _job(db_session, result.task_id)
    assert job.result["deduped"] == 1
    assert db_session.query(Memory).filter_by(content="用户对花生过敏").count() == 1
    assert db_session.query(ReviewItem).count() == 0


def test_auto_accept_can_be_disabled(
    db_session: Session, fake_llm, admin, ingest_payload, run_worker_once
) -> None:
    fake_llm.set_json(_candidate_json())

    ingest_conversation(
        db_session, ingest_payload("我吃花生会过敏", auto_accept=False), admin
    )
    run_worker_once()

    assert db_session.query(Memory).count() == 0
    assert db_session.query(ReviewItem).count() == 1


def test_ingest_endpoint_is_idempotent_and_audited(
    client, admin, workspace, session_factory
) -> None:
    body = {
        "workspace_id": workspace["id"],
        "session_id": "s-rest",
        "messages": [{"role": "user", "content": "我吃花生会过敏"}],
        "scope": "user",
        "scope_id": "u-rest",
    }
    first = client.post("/api/v1/conversations/ingest", json=body)
    second = client.post("/api/v1/conversations/ingest", json=body)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["task_id"] == second.json()["task_id"]

    with session_factory() as session:
        jobs = list(session.scalars(select(JobRun)))
        assert len(jobs) == 1
        actions = {row.action for row in session.scalars(select(AuditLog))}
    assert "ingest.created" in actions
