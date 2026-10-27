"""记忆事实源、嵌入缓存、关联与实体。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, TimestampMixin, jsonb_column, utcnow


class Memory(Base, TimestampMixin):
    """一条记忆的权威事实源；向量库与图库均为可重建投影。"""

    __tablename__ = "memory"
    __table_args__ = (
        Index("ix_memory_workspace_id", "workspace_id"),
        Index(
            "ix_memory_workspace_scope",
            "workspace_id",
            "scope",
            "scope_id",
            "status",
            "type",
        ),
        Index(
            "ix_memory_status_valid_to_last_reinforced",
            "status",
            "valid_to",
            "last_reinforced_at",
        ),
        Index("ix_memory_search_tsv", "search_tsv", postgresql_using="gin"),
        Index(
            "ix_memory_metadata",
            "metadata",
            postgresql_using="gin",
            postgresql_ops={"metadata": "jsonb_path_ops"},
        ),
        # 幂等写入：只约束非空 key，历史行与未指定 key 的写入不受影响。
        Index(
            "uq_memory_workspace_idempotency_key",
            "workspace_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        CheckConstraint(
            "scope IN ('global', 'agent', 'user', 'session')",
            name="ck_memory_scope",
        ),
        CheckConstraint(
            "type IN ('working', 'episodic', 'semantic', 'procedural', 'perceptual')",
            name="ck_memory_type",
        ),
        CheckConstraint(
            "status IN ('active', 'archived', 'superseded', 'deleted', 'purged')",
            name="ck_memory_status",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str | None] = mapped_column(String(36))
    type: Mapped[str] = mapped_column(String(16), nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))

    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    stability: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    last_reinforced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("memory.id", ondelete="SET NULL")
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual", server_default="manual"
    )
    source_id: Mapped[str | None] = mapped_column(String(64))

    metadata_: Mapped[dict[str, Any]] = jsonb_column()
    search_tsv: Mapped[Any | None] = mapped_column(TSVECTOR)
    consolidation_run_id: Mapped[str | None] = mapped_column(String(36))

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryEmbedding(Base):
    """按 (memory, model, dim) 缓存的嵌入，事实源仍为 Postgres。"""

    __tablename__ = "memory_embedding"
    __table_args__ = (UniqueConstraint("memory_id", "model", "dim"),)

    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory.id", ondelete="CASCADE"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[list[float]] = mapped_column(ARRAY(Float), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class MemoryLink(Base, CreatedAtMixin):
    __tablename__ = "memory_link"
    __table_args__ = (
        Index("ix_memory_link_from_id", "from_id"),
        Index("ix_memory_link_to_id", "to_id"),
    )

    from_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory.id", ondelete="CASCADE"), nullable=False
    )
    to_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory.id", ondelete="CASCADE"), nullable=False
    )
    relation: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Entity(Base, CreatedAtMixin):
    __tablename__ = "entity"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        Index("ix_entity_workspace_id", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str | None] = mapped_column(String(64))
    canonical_name: Mapped[str | None] = mapped_column(String(255))


class MemoryEntity(Base):
    __tablename__ = "memory_entity"
    __table_args__ = (UniqueConstraint("memory_id", "entity_id"),)

    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entity.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str | None] = mapped_column(String(32))


@event.listens_for(Memory, "before_update")
def _bump_version_on_orm_update(
    mapper: object, connection: object, target: Memory
) -> None:
    """任何 ORM 更新都自动抬升 ``version``。

    数据库触发器负责兜住绕过 ORM 的 UPDATE；这里让内存对象与数据库保持同值，
    避免后续 outbox payload 使用过期版本号。已显式修改 ``version`` 时尊重调用方。
    """
    state = inspect(target)
    if not state.attrs.version.history.has_changes():
        target.version = (target.version or 0) + 1

