"""管理员认证：首次启动 setup、登录 / 登出、当前主体查询。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.password import hash_password, needs_rehash, verify_password
from app.auth.ratelimit import (
    acquire_login_attempt,
    login_lock_seconds,
    record_login_failure,
    release_login_attempt,
    reset_login_failures,
)
from app.auth.session import (
    AuthContext,
    clear_session,
    create_session,
    read_session,
    require_admin,
    require_auth,
)
from app.db import get_db
from app.errors import ConflictError, RateLimitError, UnauthorizedError
from app.models import AdminUser, Workspace
from app.models.base import utcnow
from app.services.audit import write_audit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

ADMIN_USERNAME = "admin"

# 固定 advisory lock：serialize 首次 setup，保证进程/多实例并发下只创建一个管理员。
SETUP_ADVISORY_LOCK_ID = 0x616D7365747570
LOGIN_ATTEMPT_RETRY_AFTER = 5

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
    """首次启动创建唯一管理员；重复调用返回 409。

    先在事务级 advisory lock 内重新计数，避免两个并发 setup（不同用户名）
    各自通过 ``count(*)`` 检查后都插入成功。
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": SETUP_ADVISORY_LOCK_ID}
    )
    existing = session.scalar(select(func.count()).select_from(AdminUser))
    if existing:
        raise ConflictError("管理员已存在，请直接登录")

    admin = AdminUser(
        username=payload.username, password_hash=hash_password(payload.password)
    )
    session.add(admin)
    session.flush()
    # 首次安装即创建默认工作区，否则设置页无法创建第一个 API Key。
    if session.get(Workspace, "ws_default") is None:
        session.add(Workspace(id="ws_default", name="默认工作区"))
    write_audit(
        session,
        action="auth.setup",
        target_type="admin_user",
        target_id=admin.id,
        actor_type="system",
        after={"username": admin.username},
    )
    try:
        session.commit()
    except IntegrityError as exc:  # 理论上不可达：advisory lock 已串行化
        session.rollback()
        raise ConflictError("管理员已存在，请直接登录") from exc
    return {"admin": _admin_payload(admin)}


def _audit_login(
    session: Session,
    action: str,
    *,
    username: str,
    admin_id: str | None,
) -> None:
    write_audit(
        session,
        action=action,
        target_type="admin_user",
        target_id=admin_id or username or "unknown",
        actor_type="admin" if admin_id is not None else "anonymous",
        actor_id=admin_id,
    )


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    ip = _client_ip(request)
    attempt = acquire_login_attempt(ip)
    if attempt is None:
        # 同一 IP 已有登录校验在途：直接拒绝，避免并发绕过失败计数。
        raise RateLimitError(
            "登录请求过于频繁，请稍后再试", retry_after=LOGIN_ATTEMPT_RETRY_AFTER
        )
    try:
        locked_for = login_lock_seconds(ip)
        if locked_for:
            raise RateLimitError("登录失败次数过多，请稍后再试", retry_after=locked_for)

        admin = session.scalar(
            select(AdminUser).where(AdminUser.username == payload.username)
        )
        if admin is None:
            # 仍做一次 Argon2 校验，避免通过响应时间探测账号存在性。
            verify_password(payload.password, _DUMMY_HASH)
            record_login_failure(ip)
            _audit_login(
                session, "auth.login_failed", username=payload.username, admin_id=None
            )
            session.commit()
            raise UnauthorizedError("用户名或密码错误")
        if not verify_password(payload.password, admin.password_hash):
            record_login_failure(ip)
            _audit_login(
                session, "auth.login_failed", username=payload.username, admin_id=None
            )
            session.commit()
            raise UnauthorizedError("用户名或密码错误")

        reset_login_failures(ip)
        if needs_rehash(admin.password_hash):
            admin.password_hash = hash_password(payload.password)
        admin.last_login_at = utcnow()
        _audit_login(session, "auth.login", username=admin.username, admin_id=admin.id)
        csrf_token = create_session(response, admin.id)
        session.commit()
        return {"csrf_token": csrf_token, "admin": _admin_payload(admin)}
    finally:
        release_login_attempt(ip, attempt)


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_db),
) -> Response:
    write_audit(
        session,
        action="auth.logout",
        target_type="admin_user",
        target_id=admin.id,
        actor_type="admin",
        actor_id=admin.id,
    )
    session.commit()
    clear_session(response)
    response.status_code = 204
    return response


@router.get("/me")
def me(
    request: Request,
    context: AuthContext = Depends(require_auth),
) -> dict[str, Any]:
    """同时接受会话 Cookie 与 Bearer API Key。

    会话登录时把当次 CSRF token 一并返回，供管理台在刷新/新标签页后水合；
    API Key 无 Cookie 会话，``csrf_token`` 为 ``None``。
    """
    session_payload = read_session(request) if context.kind == "session" else None
    return {
        "kind": context.kind,
        "admin": _admin_payload(context.admin) if context.admin is not None else None,
        # 每次读取都来自签名 Cookie 中的同一 token，刷新不会改变它。
        "csrf_token": (
            session_payload.get("csrf") if session_payload is not None else None
        ),
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
