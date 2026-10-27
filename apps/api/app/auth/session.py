"""签名 Cookie 会话、CSRF 双提交与统一认证依赖。

- 会话 Cookie 用 ``itsdangerous`` 签名，``HttpOnly`` + ``SameSite=Lax``，
  生产环境额外加 ``Secure``。
- 签名载荷携带 CSRF token：登录时在响应体返回，写操作必须通过
  ``X-CSRF-Token`` 头回传（Bearer API Key 请求豁免）。
- ``/auth/me`` 既接受会话 Cookie，也接受 ``Authorization: Bearer mem_...``。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Depends, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.auth.api_key import SCOPES, has_scope, verify_api_key
from app.auth.ratelimit import enforce_api_key_rate
from app.config import get_current_settings
from app.db import get_db
from app.errors import ForbiddenError, UnauthorizedError
from app.models import AdminUser, ApiKey
from app.models.base import utcnow

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SESSION_SALT = "agent-memory.session"
ALL_SCOPES: list[str] = list(SCOPES)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_current_settings().secret_key, salt=SESSION_SALT)


def create_session(response: Response, admin_id: str) -> str:
    """写入签名会话 Cookie，返回本次会话的 CSRF token。"""
    settings = get_current_settings()
    csrf_token = secrets.token_urlsafe(32)
    token = _serializer().dumps({"admin_id": admin_id, "csrf": csrf_token})
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        path="/",
    )
    return csrf_token


def clear_session(response: Response) -> None:
    settings = get_current_settings()
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
    )


def read_session(request: Request) -> dict[str, str] | None:
    """解析并校验会话 Cookie；缺失、被篡改或过期返回 None。"""
    settings = get_current_settings()
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=settings.session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict) or "admin_id" not in payload:
        return None
    return payload


@dataclass(frozen=True)
class AuthContext:
    """一次请求的认证主体：管理员会话或工作区 API Key。"""

    kind: str
    admin: AdminUser | None = None
    api_key: ApiKey | None = None

    @property
    def workspace_id(self) -> str | None:
        return self.api_key.workspace_id if self.api_key is not None else None

    @property
    def scopes(self) -> list[str]:
        if self.admin is not None:
            return ALL_SCOPES
        return list(self.api_key.scopes) if self.api_key is not None else []

    def has_scope(self, required: str) -> bool:
        if self.admin is not None:
            return True
        return has_scope(self.scopes, required)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthorizedError("Authorization 头格式无效")
    return token.strip()


def require_admin(request: Request, session: Session = Depends(get_db)) -> AdminUser:
    """仅接受会话 Cookie，用于登录态专属操作（如 logout）。"""
    if request.headers.get("Authorization"):
        raise ForbiddenError("该端点只接受管理台会话")
    payload = read_session(request)
    if payload is None:
        raise UnauthorizedError()
    admin = session.get(AdminUser, payload["admin_id"])
    if admin is None:
        raise UnauthorizedError("会话已失效")
    _check_csrf(request, payload)
    return admin


def require_auth(request: Request, session: Session = Depends(get_db)) -> AuthContext:
    """接受 ``Authorization: Bearer`` 或会话 Cookie，失败统一 401。"""
    token = _bearer_token(request)
    if token is not None:
        api_key = verify_api_key(session, token)
        if api_key is None:
            raise UnauthorizedError("API Key 无效或已吊销")
        enforce_api_key_rate(
            api_key.id,
            write=request.method not in SAFE_METHODS,
            override=api_key.rate_limit,
        )
        api_key.last_used_at = utcnow()
        session.commit()
        return AuthContext(kind="api_key", api_key=api_key)

    payload = read_session(request)
    if payload is None:
        raise UnauthorizedError()
    admin = session.get(AdminUser, payload["admin_id"])
    if admin is None:
        raise UnauthorizedError("会话已失效")
    _check_csrf(request, payload)
    return AuthContext(kind="session", admin=admin)


def _check_csrf(request: Request, payload: dict[str, str]) -> None:
    if request.method in SAFE_METHODS:
        return
    provided = request.headers.get("X-CSRF-Token", "")
    expected = payload.get("csrf", "")
    if not provided or not expected or not secrets.compare_digest(provided, expected):
        raise ForbiddenError("CSRF 校验失败", code="csrf_failed")


def require_scope(required: str):
    """依赖工厂：要求会话管理员或具备指定 scope 的 API Key。"""
    if required not in SCOPES:
        raise ValueError(f"未知 scope: {required}")

    def dependency(context: AuthContext = Depends(require_auth)) -> AuthContext:
        if not context.has_scope(required):
            raise ForbiddenError(f"需要 {required} 权限")
        return context

    return dependency
