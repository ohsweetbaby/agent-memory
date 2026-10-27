"""SQLAlchemy 声明基类与通用列工具。

主键统一使用 UUIDv7 字符串（RFC 9562），既保留时间有序性，也便于与
Qdrant point id、Neo4j 节点 id 直接对齐。Python 3.12 标准库尚未提供
``uuid.uuid7``，这里按规范手工拼装 48 位毫秒时间戳与 74 位随机数。
"""

from __future__ import annotations

import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 固定约束命名，保证 Alembic autogenerate 与手工迁移的索引名一致。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def new_uuid7() -> str:
    """生成 UUIDv7 字符串。"""
    timestamp_ms = time.time_ns() // 1_000_000
    value = (timestamp_ms & 0xFFFFFFFFFFFF) << 80
    value |= 0x7 << 76  # version = 7
    value |= secrets.randbits(12) << 64  # rand_a
    value |= 0x2 << 62  # RFC 4122 variant
    value |= secrets.randbits(62)  # rand_b
    return str(uuid.UUID(int=value))


def utcnow() -> datetime:
    """时区感知的当前时间，所有 timestamptz 列的 Python 侧默认值。"""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """所有 ORM 模型的共同基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_uuid7
    )


class CreatedAtMixin:
    """带创建时间的表。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )


class TimestampMixin(CreatedAtMixin):
    """同时维护创建时间与更新时间；更新时由 ORM 自动刷新。"""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


def jsonb_column(
    *,
    nullable: bool = False,
    default: Any = dict,
    column_name: str = "metadata",
):
    """JSONB 列工厂。

    ``metadata`` 是 SQLAlchemy 声明类的保留属性名，因此 Python 属性统一加
    下划线（``metadata_``），数据库列名仍保持规格中的 ``metadata``。
    """
    return mapped_column(
        column_name,
        JSONB,
        nullable=nullable,
        default=default,
        server_default=text("'{}'::jsonb"),
    )
