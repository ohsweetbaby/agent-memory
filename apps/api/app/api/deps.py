"""API 层共享依赖：workspace 归属校验与游标分页。"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Select, tuple_
from sqlalchemy.orm import Session

from app.auth.session import AuthContext
from app.errors import BadRequestError, NotFoundError
from app.models import Workspace

ModelT = TypeVar("ModelT")

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


def resolve_workspace_id(context: AuthContext, requested: str | None) -> str:
    """确定本次写入所属的 workspace；API Key 只能写入自己的 workspace。"""
    if requested:
        if context.workspace_id is not None and requested != context.workspace_id:
            # 跨 workspace 一律 404，避免枚举。
            raise NotFoundError("工作区不存在")
        return requested
    if context.workspace_id is not None:
        return context.workspace_id
    raise BadRequestError("必须指定 workspace_id", code="invalid_request")


def workspace_filter(context: AuthContext, requested: str | None) -> str | None:
    """列表查询的 workspace 过滤：会话管理员可跨工作区，Key 固定自身。"""
    if requested:
        if context.workspace_id is not None and requested != context.workspace_id:
            raise NotFoundError("工作区不存在")
        return requested
    return context.workspace_id


def ensure_workspace(session: Session, context: AuthContext, workspace_id: str) -> Workspace:
    """校验 workspace 存在且当前主体可访问，否则 404。"""
    if context.workspace_id is not None and workspace_id != context.workspace_id:
        raise NotFoundError("工作区不存在")
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFoundError("工作区不存在")
    return workspace


def encode_cursor(created_at: datetime, row_id: str) -> str:
    raw = f"{created_at.isoformat()}|{row_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        timestamp, row_id = raw.split("|", 1)
        return datetime.fromisoformat(timestamp), row_id
    except (ValueError, binascii.Error, UnicodeError) as exc:
        raise BadRequestError("游标无效", code="invalid_cursor") from exc


def paginate(
    session: Session,
    statement: Select[Any],
    model: type[ModelT],
    *,
    limit: int,
    cursor: str | None,
) -> tuple[list[ModelT], str | None]:
    """按 ``(created_at, id)`` 倒序返回一页，并提供下一页游标。"""
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise BadRequestError(f"limit 必须在 1..{MAX_PAGE_SIZE} 之间", code="invalid_limit")
    if cursor:
        created_at, row_id = decode_cursor(cursor)
        statement = statement.where(
            tuple_(model.created_at, model.id) < (created_at, row_id)  # type: ignore[attr-defined]
        )
    statement = statement.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)  # type: ignore[attr-defined]
    rows = list(session.scalars(statement))
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
    return page, next_cursor
