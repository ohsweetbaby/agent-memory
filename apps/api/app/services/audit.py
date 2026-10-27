"""审计日志写入（规格 §12）。

审计必须与业务写入处于同一事务：调用方负责 ``commit``/``rollback``，
本模块只 ``add + flush``。这样业务失败时不会留下“成功”审计，业务成功
也不会漏审计。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.auth.session import AuthContext
from app.models import AdminUser, ApiKey, AuditLog


@dataclass(frozen=True, slots=True)
class ActorRef:
    """后台任务复用的审计主体；worker 里没有请求上下文对象。"""

    kind: str
    id: str | None = None


def actor_fields(actor: Any) -> tuple[str, str | None]:
    """把认证上下文/模型/后台引用统一成 ``(actor_type, actor_id)``。"""
    if isinstance(actor, ActorRef):
        return actor.kind, actor.id
    if isinstance(actor, AuthContext):
        if actor.api_key is not None:
            return "api_key", actor.api_key.id
        if actor.admin is not None:
            return "admin", actor.admin.id
    if isinstance(actor, AdminUser):
        return "admin", actor.id
    if isinstance(actor, ApiKey):
        return "api_key", actor.id
    raise TypeError(f"无法识别的审计主体: {type(actor)!r}")


def write_audit(
    session: Session,
    *,
    action: str,
    target_type: str,
    target_id: str,
    workspace_id: str | None = None,
    actor_type: str = "admin",
    actor_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    """写入一条审计行；不提交事务。"""
    entry = AuditLog(
        workspace_id=workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
    )
    session.add(entry)
    session.flush()
    return entry


__all__ = ["ActorRef", "actor_fields", "write_audit"]
