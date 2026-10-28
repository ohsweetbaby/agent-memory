"""硬删除/到期清理时的关联数据脱敏。

`memory` 墓碑之外，审核项与任务结果也可能保存正文；硬删除必须同步清理，
否则 `/review`、`/jobs` 或导出仍能读到已承诺删除的内容。
"""

from __future__ import annotations

from sqlalchemy import Text, cast, delete, or_, select, update
from sqlalchemy.orm import Session

from app.models import AuditLog, JobRun, ReviewItem


def redact_related_records(
    db: Session, memory_id: str, workspace_id: str
) -> None:
    """删除指向该记忆的审核项/审核快照，并脱敏包含其 id 的任务结果。"""
    review_ids = list(
        db.scalars(
            select(ReviewItem.id).where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.candidate["memory_id"].astext == memory_id,
            )
        )
    )
    if review_ids:
        db.execute(
            delete(AuditLog).where(
                AuditLog.target_type == "review_item",
                AuditLog.target_id.in_(review_ids),
            )
        )
        db.execute(delete(ReviewItem).where(ReviewItem.id.in_(review_ids)))

    db.execute(
        delete(AuditLog).where(
            AuditLog.action == "consolidation.completed",
            cast(AuditLog.after, Text).contains(memory_id),
        )
    )
    db.execute(
        update(JobRun)
        .where(
            or_(
                cast(JobRun.result, Text).contains(memory_id),
                cast(JobRun.params, Text).contains(memory_id),
            )
        )
        .values(
            result={"redacted": True, "reason": "memory_purged"},
            params={"redacted": True},
        )
    )


__all__ = ["redact_related_records"]
