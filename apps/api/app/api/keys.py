"""API Key 管理：列出、创建（明文只返回一次）、吊销。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    DEFAULT_PAGE_SIZE,
    ensure_workspace,
    paginate,
    resolve_workspace_id,
    workspace_filter,
)
from app.auth.api_key import SCOPES, create_api_key, normalize_scopes
from app.auth.session import AuthContext, require_scope
from app.db import get_db
from app.errors import ApiError, ConflictError, NotFoundError
from app.models import ApiKey
from app.models.base import utcnow

router = APIRouter(prefix="/api/v1/keys", tags=["keys"])


class ApiKeyCreate(BaseModel):
    workspace_id: str | None = Field(default=None, max_length=36)
    name: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(min_length=1)
    rate_limit: int | None = Field(default=None, ge=1)


def _key_payload(key: ApiKey) -> dict[str, Any]:
    """永远不返回 ``key_hash``。"""
    return {
        "id": key.id,
        "workspace_id": key.workspace_id,
        "name": key.name,
        "prefix": key.prefix,
        "scopes": list(key.scopes),
        "rate_limit": key.rate_limit,
        "created_at": key.created_at,
        "last_used_at": key.last_used_at,
        "revoked_at": key.revoked_at,
    }


@router.get("")
def list_keys(
    workspace_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=DEFAULT_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    resolved = workspace_filter(context, workspace_id)
    statement = select(ApiKey)
    if resolved is not None:
        ensure_workspace(session, context, resolved)
        statement = statement.where(ApiKey.workspace_id == resolved)
    items, next_cursor = paginate(
        session, statement, ApiKey, limit=limit, cursor=cursor
    )
    return {"items": [_key_payload(item) for item in items], "next_cursor": next_cursor}


@router.post("", status_code=201)
def create_key(
    payload: ApiKeyCreate,
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace_id = resolve_workspace_id(context, payload.workspace_id)
    ensure_workspace(session, context, workspace_id)
    try:
        normalized = normalize_scopes(payload.scopes)
    except ValueError as exc:
        raise ApiError(str(exc), code="invalid_scope") from exc

    if context.api_key is not None:
        # 防止用 Key 自身提权：Key 只能创建 scope 不超过自身的 Key。
        for scope in normalized:
            if not context.has_scope(scope):
                raise ApiError("不能创建权限高于自身的 API Key", code="forbidden")

    key, raw = create_api_key(
        session,
        workspace_id,
        payload.name,
        normalized,
        rate_limit=payload.rate_limit,
    )
    try:
        session.commit()
    except IntegrityError as exc:  # 前缀碰撞概率极低，重试由调用方决定
        session.rollback()
        raise ConflictError("API Key 前缀冲突，请重试") from exc
    return {**_key_payload(key), "key": raw}


@router.delete("/{key_id}", status_code=204)
def revoke_key(
    key_id: str,
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> Response:
    key = session.get(ApiKey, key_id)
    if key is None:
        raise NotFoundError("API Key 不存在")
    if context.workspace_id is not None and key.workspace_id != context.workspace_id:
        raise NotFoundError("API Key 不存在")
    if key.revoked_at is not None:
        raise NotFoundError("API Key 不存在")
    key.revoked_at = utcnow()
    session.commit()
    return Response(status_code=204)


# 导出合法 scope 供 OpenAPI/前端使用，避免魔法字符串散落。
VALID_SCOPES = SCOPES
