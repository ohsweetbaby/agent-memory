"""管理员认证：首次启动 setup、登录 / 登出、当前主体查询。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.password import hash_password, needs_rehash, verify_password
from app.auth.ratelimit import (
    login_lock_seconds,
    record_login_failure,
    reset_login_failures,
)
from app.auth.session import (
    AuthContext,
    clear_session,
    create_session,
    require_admin,
    require_auth,
)
from app.db import get_db
from app.errors import ConflictError, RateLimitError, UnauthorizedError
from app.models import AdminUser
from app.models.base import utcnow

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

ADMIN_USERNAME = "admin"

# 用户名不存在时也做一次 Argon2 校验，避免通过响应时间探测账号存在性。
_DUMMY_HASH = hash_password("not-a-real-password")


class SetupRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)
    username: str = Field(default=ADMIN_USERNAME, min_length=1, max_length=64)


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    username: str = Field(default=ADMIN_USERNAME, min_length=1, max_length=64)


def _admin_payload(admin: AdminUser) -> dict[str, Any]:
    return {
        "id": admin.id,
        "username": admin.username,
        "created_at": admin.created_at,
        "last_login_at": admin.last_login_at,
    }


def _client_ip(request: Request) -> str:
    # 只信任直连地址；反向代理场景由部署层负责注入并限制来源。
    return request.client.host if request.client is not None else "unknown"


@router.post("/setup", status_code=201)
def setup(payload: SetupRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    """首次启动创建唯一管理员；重复调用返回 409。"""
    existing = session.scalar(select(func.count()).select_from(AdminUser))
    if existing:
        raise ConflictError("管理员已存在，请直接登录")
    admin = AdminUser(
        username=payload.username, password_hash=hash_password(payload.password)
    )
    session.add(admin)
    try:
        session.commit()
    except IntegrityError as exc:  # 并发 setup 时唯一约束兜底
        session.rollback()
        raise ConflictError("管理员已存在，请直接登录") from exc
    return {"admin": _admin_payload(admin)}


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    ip = _client_ip(request)
    locked_for = login_lock_seconds(ip)
    if locked_for:
        raise RateLimitError("登录失败次数过多，请稍后再试", retry_after=locked_for)

    admin = session.scalar(select(AdminUser).where(AdminUser.username == payload.username))
    if admin is None:
        verify_password(payload.password, _DUMMY_HASH)
        record_login_failure(ip)
        raise UnauthorizedError("用户名或密码错误")
    if not verify_password(payload.password, admin.password_hash):
        record_login_failure(ip)
        raise UnauthorizedError("用户名或密码错误")

    reset_login_failures(ip)
    if needs_rehash(admin.password_hash):
        admin.password_hash = hash_password(payload.password)
    admin.last_login_at = utcnow()
    csrf_token = create_session(response, admin.id)
    session.commit()
    return {"csrf_token": csrf_token, "admin": _admin_payload(admin)}


@router.post("/logout", status_code=204)
def logout(response: Response, admin: AdminUser = Depends(require_admin)) -> Response:
    clear_session(response)
    response.status_code = 204
    return response


@router.get("/me")
def me(
    context: AuthContext = Depends(require_auth),
) -> dict[str, Any]:
    """同时接受会话 Cookie 与 Bearer API Key。"""
    return {
        "kind": context.kind,
        "admin": _admin_payload(context.admin) if context.admin is not None else None,
        "api_key": (
            {
                "id": context.api_key.id,
                "workspace_id": context.api_key.workspace_id,
                "name": context.api_key.name,
                "prefix": context.api_key.prefix,
                "scopes": context.api_key.scopes,
            }
            if context.api_key is not None
            else None
        ),
        "scopes": context.scopes,
    }
