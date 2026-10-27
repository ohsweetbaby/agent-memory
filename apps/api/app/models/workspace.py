"""工作空间、Agent、终端用户与会话。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, jsonb_column, utcnow


class Workspace(Base, CreatedAtMixin):
    __tablename__ = "workspace"

    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Agent(Base, CreatedAtMixin):
    __tablename__ = "agent"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        Index("ix_agent_workspace_id", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EndUser(Base, CreatedAtMixin):
    __tablename__ = "end_user"
    __table_args__ = (
        UniqueConstraint("workspace_id", "external_id"),
        Index("ix_end_user_workspace_id", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Session(Base, CreatedAtMixin):
    __tablename__ = "session"
    __table_args__ = (Index("ix_session_workspace_id", "workspace_id"),)

    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent.id", ondelete="SET NULL")
    )
    end_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("end_user.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = jsonb_column()


class Episode(Base, CreatedAtMixin):
    __tablename__ = "episode"
    __table_args__ = (Index("ix_episode_session_id", "session_id"),)

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(64))
    metadata_: Mapped[dict[str, Any]] = jsonb_column()
