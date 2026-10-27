"""审计日志写入（规格 §12）。

审计必须与业务写入处于同一事务：调用方负责 ``commit``/``rollback``，
本模块只 ``add + flush``。这样业务失败时不会留下“成功”审计，业务成功
也不会漏审计。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


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


__all__ = ["write_audit"]
