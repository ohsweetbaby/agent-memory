"""硬删除关联数据脱敏：审核项、审核审计与巩固任务快照。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, JobRun, ReviewItem
from app.services.privacy import redact_related_records


def test_redact_related_records_removes_review_and_consolidation_snapshots(
    db_session: Session,
) -> None:
    review = ReviewItem(
        workspace_id="ws-privacy",
        candidate={"memory_id": "mem-1", "content": "review secret"},
        evidence={"source_quote": "review secret"},
        status="approved",
    )
    job = JobRun(
        type="consolidate",
        status="completed",
        result={"actions": [{"memory_id": "mem-1", "content": "job secret"}]},
    )
    audit = AuditLog(
        workspace_id="ws-privacy",
        actor_type="system",
        action="consolidation.completed",
        target_type="consolidation",
        target_id="job-1",
        after={"actions": [{"memory_id": "mem-1", "content": "audit secret"}]},
    )
    db_session.add_all([review, job, audit])
    db_session.flush()

    redact_related_records(db_session, "mem-1", "ws-privacy")
    db_session.flush()

    assert db_session.scalars(select(ReviewItem)).all() == []
    assert db_session.scalars(select(JobRun)).one().result == {
        "redacted": True,
        "reason": "memory_purged",
    }
    assert db_session.scalars(select(AuditLog)).all() == []
