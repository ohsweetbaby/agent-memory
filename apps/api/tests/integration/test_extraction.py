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
from app.models import AuditLog, Entity, JobRun, Memory, ReviewItem
from app.models.base import utcnow
from app.schemas.memory import (
    ValidationError as ContentValidationError,
    ensure_no_nul,
)
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

    memory = db_session.query(Memory).filter_by(content="用户对花生过敏").one()
    assert memory.source_type == "extraction"
    assert db_session.query(ReviewItem).count() == 0
    assert "memory.created" in {
        row.action for row in db_session.scalars(select(AuditLog))
    }
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


def test_llm_schema_root_is_object_and_parses_candidates_object(
    db_session: Session, fake_llm, admin, ingest_payload, run_worker_once
) -> None:
    """Structured Outputs 不接受数组根；解析器同时兼容 candidates 对象。"""
    fake_llm.set_json({"candidates": _candidate_json()})

    ingest_conversation(db_session, ingest_payload("我吃花生会过敏"), admin)
    run_worker_once()

    schema = fake_llm.last_schema or {}
    assert schema["type"] == "object"
    assert schema["properties"]["candidates"]["type"] == "array"
    assert db_session.query(Memory).filter_by(content="用户对花生过敏").count() == 1


@pytest.mark.parametrize(
    ("confidence", "importance"),
    [(0.95, 0.2), (0.2, 0.95)],
)
def test_auto_accept_requires_both_thresholds(
    db_session: Session,
    fake_llm,
    admin,
    ingest_payload,
    run_worker_once,
    confidence: float,
    importance: float,
) -> None:
    fake_llm.set_json(
        _candidate_json(
            "单阈值候选",
            confidence=confidence,
            importance=importance,
        )
    )

    ingest_conversation(db_session, ingest_payload("单阈值对话"), admin)
    run_worker_once()

    assert db_session.query(Memory).count() == 0
    assert db_session.query(ReviewItem).count() == 1


def test_dead_letter_truncates_conversation_messages(
    db_session: Session, fake_llm, admin, ingest_payload, run_worker_once
) -> None:
    fake_llm.set_text("not json")
    messages = [
        {"role": "user", "content": "长" * 3000 + str(index)}
        for index in range(6)
    ]

    result = ingest_conversation(
        db_session, ingest_payload(messages=messages), admin
    )
    run_worker_once()

    job = _job(db_session, result.task_id)
    assert job.status == "dead"
    assert job.params["redacted"] is True
    assert len(job.params["messages"]) == 5
    assert all(
        len(message["content"].encode("utf-8")) <= 2048
        for message in job.params["messages"]
    )


def test_candidate_with_nul_evidence_is_skipped(
    db_session: Session, fake_llm, admin, ingest_payload, run_worker_once
) -> None:
    fake_llm.set_json(_candidate_json(evidence="证据\u0000片段"))

    result = ingest_conversation(db_session, ingest_payload("我吃花生会过敏"), admin)
    run_worker_once()

    job = _job(db_session, result.task_id)
    assert job.status == "completed"
    assert job.result["skipped"] == 1
    assert db_session.query(Memory).count() == 0
    assert db_session.query(ReviewItem).count() == 0


def test_reingesting_dead_letter_restores_full_messages(
    db_session: Session, fake_llm, admin, ingest_payload, run_worker_once
) -> None:
    """死信被裁剪后，同一对话重新 ingest 必须回填完整消息再重跑。"""
    messages = [
        {"role": "user", "content": f"第{index}条：" + "历史" * 1000}
        for index in range(6)
    ]
    messages.append(
        {
            "role": "user",
            "content": "最新关键信息：用户对花生过敏" + "补充" * 1000,
        }
    )
    payload = ingest_payload(messages=messages)

    fake_llm.set_text("not json")
    first = ingest_conversation(db_session, payload, admin)
    run_worker_once()

    job = _job(db_session, first.task_id)
    assert job.status == "dead"
    assert job.params["redacted"] is True
    assert len(job.params["messages"]) == 5

    fake_llm.last_messages = []
    fake_llm.set_json(_candidate_json(evidence="最新关键信息"))
    second = ingest_conversation(db_session, payload, admin)

    assert second.task_id == first.task_id
    job = _job(db_session, second.task_id)
    assert job.status == "pending"
    assert "redacted" not in job.params
    assert len(job.params["messages"]) == len(messages)
    assert job.params["message_count"] == len(messages)

    run_worker_once()
    assert "最新关键信息：用户对花生过敏" in fake_llm.last_messages[-1]["content"]
    assert db_session.query(Memory).filter_by(content="用户对花生过敏").count() == 1


def test_ensure_no_nul_rejects_all_unstorable_characters() -> None:
    """与 normalize_content 共用同一套校验：NUL 与孤立代理对都 422。"""
    with pytest.raises(ContentValidationError) as nul_exc:
        ensure_no_nul("a\u0000b", "messages[0].content")
    assert nul_exc.value.resolved_code == "invalid_character"
    assert nul_exc.value.details["character"] == "\\u0000"

    with pytest.raises(ContentValidationError) as surrogate_exc:
        ensure_no_nul("用户\ud800过敏", "messages[0].content")
    assert surrogate_exc.value.resolved_code == "invalid_character"
    assert surrogate_exc.value.details["character"] == "\\ud800"


def test_long_entity_name_is_truncated(
    db_session: Session, fake_llm, admin, ingest_payload, run_worker_once
) -> None:
    fake_llm.set_json(
        _candidate_json(
            entities=[
                {"name": "实" * 300, "type": "concept", "role": "subject"}
            ]
        )
    )

    ingest_conversation(db_session, ingest_payload("我吃花生会过敏"), admin)
    run_worker_once()

    entity = db_session.scalars(select(Entity)).one()
    assert len(entity.name) == 255
    assert db_session.query(Memory).count() == 1


def test_ingest_rejects_nul_in_request_fields(
    client, admin, workspace
) -> None:
    body = {
        "workspace_id": workspace["id"],
        "session_id": "s\u0000rest",
        "messages": [{"role": "user", "content": "我吃花生会过敏"}],
        "scope": "user",
        "scope_id": "u-rest",
    }
    r = client.post("/api/v1/conversations/ingest", json=body)

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_character"


def test_ingest_rejects_nul_in_message_ts(
    client, admin, workspace
) -> None:
    body = {
        "workspace_id": workspace["id"],
        "session_id": "s-rest",
        "messages": [
            {"role": "user", "content": "我吃花生会过敏", "ts": "2026-\u0000"}
        ],
        "scope": "user",
        "scope_id": "u-rest",
    }
    r = client.post("/api/v1/conversations/ingest", json=body)

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_character"
