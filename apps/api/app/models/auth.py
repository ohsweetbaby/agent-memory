"""API Key 与模型 Provider 配置。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, TimestampMixin, jsonb_column, utcnow


class AdminUser(Base, TimestampMixin):
    """管理台管理员；首次启动由 ``/auth/setup`` 创建。"""

    __tablename__ = "admin_user"
    __table_args__ = (UniqueConstraint("username"),)

    username: Mapped[str] = mapped_column(String(64), nullable=False)
    # Argon2id 哈希；永远不保存明文。
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiKey(Base, CreatedAtMixin):
    __tablename__ = "api_key"
    __table_args__ = (
        Index("ix_api_key_workspace_id", "workspace_id"),
        UniqueConstraint("prefix"),
    )

    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    rate_limit: Mapped[int | None] = mapped_column(Integer)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderConfig(Base):
    __tablename__ = "provider_config"
    __table_args__ = (UniqueConstraint("kind", "name"),)

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(1024))
    model: Mapped[str | None] = mapped_column(String(255))
    dim: Mapped[int | None] = mapped_column(Integer)
    extra: Mapped[dict[str, Any]] = jsonb_column(column_name="extra")
    # Fernet 密文，密钥由 SECRET_KEY 派生；数据库中不保存明文。
    encrypted_secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
