"""待审队列服务：批准、拒绝、批量操作与过期清理。

批准必须复用 :func:`app.services.extraction.accept_candidate`，从而继续走
``write_memory`` 的完整写入管线（去重/冲突/审计/outbox）。管理员可以在批准
时覆盖内容与重要性；拒绝只更新状态并写审计。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from memory_core.types import MemoryType, Scope

from app.config import get_current_settings
from app.errors import ConflictError
from app.models import AuditLog, ReviewItem
from app.models.base import utcnow
from app.schemas.memory import EntitySpec, ValidationError, ensure_no_nul, normalize_content
from app.services.audit import actor_fields, write_audit
from app.services.extraction import Candidate, accept_candidate

logger = logging.getLogger(__name__)

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
REVIEW_STATUSES = (PENDING, APPROVED, REJECTED)


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    memory_id: str
    action: str
    superseded_ids: tuple[str, ...] = ()
    item: ReviewItem | None = None


@dataclass(frozen=True, slots=True)
class BulkResult:
    approved: int = 0
    rejected: int = 0
    memory_ids: tuple[str, ...] = ()


def _entity_specs(raw_candidate: dict[str, Any]) -> tuple[EntitySpec, ...]:
    entities: list[EntitySpec] = []
    for raw in raw_candidate.get("entities") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        entity_type = raw.get("type")
        role = raw.get("role")
        type_text = str(entity_type).strip() if entity_type else None
        role_text = str(role).strip() if role else None
        ensure_no_nul(name, "entities.name")
        ensure_no_nul(type_text, "entities.type")
        ensure_no_nul(role_text, "entities.role")
        entities.append(
            EntitySpec(
                name=name[:255],
                type=type_text[:64] if type_text else None,
                role=role_text[:32] if role_text else None,
            )
        )
    return tuple(entities)


def _resolve_scope(
    raw_candidate: dict[str, Any], item: ReviewItem
) -> tuple[Scope, str | None]:
    raw_scope = raw_candidate.get("scope") or Scope.SESSION.value
    try:
        scope = Scope(raw_scope)
    except ValueError as exc:
        raise ValidationError(
            "候选作用域不合法", details={"field": "scope"}
        ) from exc
    if scope is Scope.GLOBAL:
        raise ValidationError(
            "抽取候选不支持 global 作用域", details={"field": "scope"}
        )
    scope_id = raw_candidate.get("scope_id")
    if scope_id is None:
        scope_id = raw_candidate.get("session_id") or item.id
    return scope, str(scope_id)


def candidate_from_item(
    item: ReviewItem,
    *,
    content: str | None = None,
    importance: float | None = None,
    memory_type: MemoryType | None = None,
) -> Candidate:
    """把待审项还原成候选；批准时允许覆盖内容/重要性/类型。"""
    raw = dict(item.candidate or {})
    resolved_content = normalize_content(
        content if content is not None else str(raw.get("content") or "")
    )

    if memory_type is not None:
        resolved_type = memory_type.value
    else:
        resolved_type = str(raw.get("type") or MemoryType.SEMANTIC.value)
        if resolved_type not in {item.value for item in MemoryType}:
            raise ValidationError(
                "候选类型不合法", details={"field": "type"}
            )

    if importance is not None:
        resolved_importance = float(importance)
    else:
        try:
            resolved_importance = float(raw.get("importance", 0.5))
        except (TypeError, ValueError):
            resolved_importance = 0.5
    if not 0.0 <= resolved_importance <= 1.0:
        raise ValidationError("重要性必须在 0..1", details={"field": "importance"})

    scope, scope_id = _resolve_scope(raw, item)
    evidence = item.evidence or {}
    quote = raw.get("evidence") or evidence.get("quote")
    session_id = raw.get("session_id") or evidence.get("session_id")

    ensure_no_nul(scope_id, "scope_id")
    ensure_no_nul(str(session_id) if session_id else None, "session_id")
    ensure_no_nul(str(quote) if quote else None, "evidence")

    try:
        confidence = float(raw.get("confidence", item.confidence))
    except (TypeError, ValueError):
        confidence = float(item.confidence)
    if not 0.0 <= confidence <= 1.0:
        confidence = float(item.confidence)

    return Candidate(
        content=resolved_content,
        type=resolved_type,
        importance=resolved_importance,
        confidence=confidence,
        evidence=str(quote)[:2000] if quote else None,
        entities=_entity_specs(raw),
        workspace_id=item.workspace_id,
        scope=scope,
        scope_id=scope_id,
        session_id=str(session_id) if session_id else None,
        idempotency_key=f"review:{item.id}",
    )


def approve_review_item(
    db: Session,
    item: ReviewItem,
    actor: Any,
    *,
    content: str | None = None,
    importance: float | None = None,
    memory_type: MemoryType | None = None,
    note: str | None = None,
) -> ApprovalResult:
    """批准待审项；重复批准返回已写入的记忆，不重复创建。"""
    ensure_no_nul(note, "note")
    if item.status == APPROVED:
        memory_id = (item.candidate or {}).get("memory_id")
        if memory_id:
            return ApprovalResult(
                memory_id=str(memory_id),
                action=str((item.candidate or {}).get("action") or "created"),
                item=item,
            )
        raise ConflictError("待审项已批准")
    if item.status != PENDING:
        raise ConflictError("待审项已处理")

    before_candidate = dict(item.candidate or {})
    candidate = candidate_from_item(
        item,
        content=content,
        importance=importance,
        memory_type=memory_type,
    )
    result = accept_candidate(db, candidate, actor, commit=False)

    item.candidate = {
        **before_candidate,
        "content": candidate.content,
        "type": candidate.type,
        "importance": candidate.importance,
        "memory_id": result.id,
        "action": result.action,
    }
    item.status = APPROVED
    item.reviewed_at = utcnow()

    actor_type, actor_id = actor_fields(actor)
    write_audit(
        db,
        action="review.approved",
        target_type="review_item",
        target_id=item.id,
        workspace_id=item.workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        before={"status": PENDING, "candidate": before_candidate},
        after={
            "status": APPROVED,
            "memory_id": result.id,
            "action": result.action,
            "content": candidate.content,
            "importance": candidate.importance,
            "note": note,
        },
    )
    # 记忆、待审状态与审核审计在同一事务提交，避免“记忆已写、仍 pending”。
    db.commit()
    return ApprovalResult(
        memory_id=result.id,
        action=result.action,
        superseded_ids=result.superseded_ids,
        item=item,
    )


def reject_review_item(
    db: Session,
    item: ReviewItem,
    actor: Any,
    *,
    reason: str | None = None,
) -> ReviewItem:
    """拒绝待审项；重复拒绝保持幂等，已批准则冲突。"""
    ensure_no_nul(reason, "reason")
    if item.status == REJECTED:
        return item
    if item.status != PENDING:
        raise ConflictError("待审项已处理")

    before_candidate = dict(item.candidate or {})
    item.status = REJECTED
    item.reviewed_at = utcnow()

    actor_type, actor_id = actor_fields(actor)
    write_audit(
        db,
        action="review.rejected",
        target_type="review_item",
        target_id=item.id,
        workspace_id=item.workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        before={"status": PENDING, "candidate": before_candidate},
        after={"status": REJECTED, "reason": reason},
    )
    db.commit()
    return item


def bulk_review_items(
    db: Session,
    items: list[ReviewItem],
    action: str,
    actor: Any,
    *,
    reason: str | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> BulkResult:
    """批量批准/拒绝；先整体校验状态，避免处理到一半才 409。

    ``overrides`` 以待审项 id 为键，批量通过时逐条覆盖内容与重要性；
    未提供的条目仍使用抽取候选。
    """
    ensure_no_nul(reason, "reason")
    if action not in {"approve", "reject"}:
        raise ValidationError("action 必须是 approve 或 reject")
    if not items:
        raise ValidationError("至少选择一条待审项")
    if len({item.workspace_id for item in items}) > 1:
        raise ValidationError("批量操作不能跨工作区")

    pending = [item for item in items if item.status == PENDING]
    if len(pending) != len(items):
        raise ConflictError("部分待审项已处理，请刷新后重试")

    approved = 0
    rejected = 0
    memory_ids: list[str] = []
    for item in items:
        if action == "approve":
            override = (overrides or {}).get(item.id)
            result = approve_review_item(
                db,
                item,
                actor,
                content=getattr(override, "content", None),
                importance=getattr(override, "importance", None),
            )
            approved += 1
            memory_ids.append(result.memory_id)
        else:
            reject_review_item(db, item, actor, reason=reason)
            rejected += 1

    return BulkResult(
        approved=approved,
        rejected=rejected,
        memory_ids=tuple(memory_ids),
    )


def purge_expired_review_items(
    db: Session,
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> int:
    """删除超过保留期的待审项（含已处理和未处理），供 T16 清理任务调用。"""
    days = retention_days or get_current_settings().review_retention_days
    cutoff = (now or utcnow()) - timedelta(days=days)
    expired_ids = select(ReviewItem.id).where(ReviewItem.created_at < cutoff)
    db.execute(
        delete(AuditLog).where(
            AuditLog.target_type == "review_item",
            AuditLog.target_id.in_(expired_ids),
        )
    )
    result = db.execute(delete(ReviewItem).where(ReviewItem.created_at < cutoff))
    db.commit()
    removed = int(result.rowcount or 0)
    if removed:
        logger.info("expired review items purged", extra={"count": removed})
    return removed


def review_item_payload(item: ReviewItem) -> dict[str, Any]:
    """REST 与 Web 共用的待审项视图。"""
    candidate = dict(item.candidate or {})
    return {
        "id": item.id,
        "workspace_id": item.workspace_id,
        "status": item.status,
        "content": candidate.get("content"),
        "type": candidate.get("type"),
        "importance": candidate.get("importance"),
        "confidence": item.confidence,
        "evidence": item.evidence,
        "entities": candidate.get("entities") or [],
        "scope": candidate.get("scope"),
        "scope_id": candidate.get("scope_id"),
        "candidate": candidate,
        "created_at": item.created_at,
        "reviewed_at": item.reviewed_at,
    }


__all__ = [
    "APPROVED",
    "ApprovalResult",
    "BulkResult",
    "PENDING",
    "REJECTED",
    "approve_review_item",
    "bulk_review_items",
    "candidate_from_item",
    "purge_expired_review_items",
    "reject_review_item",
    "review_item_payload",
]
