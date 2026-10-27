"""待审项、后台任务、审计、Outbox 与投影幂等状态。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, utcnow


class ReviewItem(Base, CreatedAtMixin):
    __tablename__ = "review_item"
    __table_args__ = (Index("ix_review_item_workspace_status", "workspace_id", "status"),)

    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    candidate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobRun(Base, CreatedAtMixin):
    __tablename__ = "job_run"
    __table_args__ = (Index("ix_job_run_type_status", "type", "status"),)

    type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base, CreatedAtMixin):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index(
            "ix_audit_log_target",
            "target_type",
            "target_id",
            text("created_at DESC"),
        ),
    )

    workspace_id: Mapped[str | None] = mapped_column(String(36))
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class OutboxEvent(Base, CreatedAtMixin):
    __tablename__ = "outbox_event"
    __table_args__ = (
        Index("ix_outbox_event_status_next_attempt_at", "status", "next_attempt_at"),
    )

    aggregate_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="memory", server_default="memory"
    )
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class ProjectionState(Base):
    __tablename__ = "projection_state"
    __table_args__ = (UniqueConstraint("memory_id", "target"),)

    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory.id", ondelete="CASCADE"), nullable=False
    )
    target: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    projected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    error: Mapped[str | None] = mapped_column(Text)
