"""待审队列 REST：列表、批准、拒绝与批量操作（规格 §8）。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_core.types import MemoryType

from app.api.deps import (
    DEFAULT_PAGE_SIZE,
    ensure_workspace,
    paginate,
    workspace_filter,
)
from app.auth.session import AuthContext, require_scope
from app.db import get_db
from app.errors import BadRequestError, NotFoundError
from app.models import ReviewItem
from app.schemas.memory import ensure_no_nul
from app.services.review import (
    APPROVED,
    PENDING,
    REJECTED,
    approve_review_item,
    bulk_review_items,
    reject_review_item,
    review_item_payload,
)

router = APIRouter(prefix="/api/v1/review", tags=["review"])


class ApproveRequest(BaseModel):
    """批准时可选编辑；缺省沿用抽取候选。"""

    content: str | None = Field(default=None, max_length=8192)
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    type: MemoryType | None = None
    note: str | None = Field(default=None, max_length=2000)


class RejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class BulkRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    action: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=2000)


def _load_item(session: Session, context: AuthContext, item_id: str) -> ReviewItem:
    """跨 workspace 与不存在统一 404，避免枚举；加行锁串行化审核。"""
    ensure_no_nul(item_id, "id")
    item = session.scalar(
        select(ReviewItem)
        .where(ReviewItem.id == item_id)
        .with_for_update()
    )
    if item is None:
        raise NotFoundError("待审项不存在")
    if context.workspace_id is not None and item.workspace_id != context.workspace_id:
        raise NotFoundError("待审项不存在")
    return item


@router.get("")
def list_review_items(
    workspace_id: str | None = Query(default=None, max_length=36),
    status: str = Query(default=PENDING),
    limit: int = Query(default=DEFAULT_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    resolved_status = status.strip().lower()
    if resolved_status not in {PENDING, APPROVED, REJECTED, "all"}:
        raise BadRequestError(
            "status 必须是 pending/approved/rejected/all",
            code="invalid_status",
        )
    ensure_no_nul(workspace_id, "workspace_id")

    resolved = workspace_filter(context, workspace_id)
    statement = select(ReviewItem)
    if resolved is not None:
        ensure_workspace(session, context, resolved)
        statement = statement.where(ReviewItem.workspace_id == resolved)
    if resolved_status != "all":
        statement = statement.where(ReviewItem.status == resolved_status)

    items, next_cursor = paginate(
        session, statement, ReviewItem, limit=limit, cursor=cursor
    )
    return {
        "items": [review_item_payload(item) for item in items],
        "next_cursor": next_cursor,
    }


@router.post("/bulk")
def bulk_review(
    payload: BulkRequest,
    context: AuthContext = Depends(require_scope("write")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    if len(set(payload.ids)) != len(payload.ids):
        raise BadRequestError("ids 不能重复", code="duplicate_ids")
    for index, item_id in enumerate(payload.ids):
        ensure_no_nul(item_id, f"ids[{index}]")

    items = list(
        session.scalars(
            select(ReviewItem)
            .where(ReviewItem.id.in_(payload.ids))
            .with_for_update()
        )
    )
    found = {item.id for item in items}
    missing = [item_id for item_id in payload.ids if item_id not in found]
    if missing:
        raise NotFoundError("待审项不存在")
    for item in items:
        if context.workspace_id is not None and item.workspace_id != context.workspace_id:
            raise NotFoundError("待审项不存在")

    result = bulk_review_items(
        session,
        items,
        payload.action,
        context,
        reason=payload.reason,
    )
    return {
        "approved": result.approved,
        "rejected": result.rejected,
        "memory_ids": list(result.memory_ids),
    }


@router.post("/{item_id}/approve")
def approve_item(
    item_id: str,
    payload: ApproveRequest,
    context: AuthContext = Depends(require_scope("write")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    item = _load_item(session, context, item_id)
    result = approve_review_item(
        session,
        item,
        context,
        content=payload.content,
        importance=payload.importance,
        memory_type=payload.type,
        note=payload.note,
    )
    return {
        "review_item_id": item.id,
        "memory_id": result.memory_id,
        "action": result.action,
        "superseded_ids": list(result.superseded_ids),
        "status": item.status,
    }


@router.post("/{item_id}/reject")
def reject_item(
    item_id: str,
    payload: RejectRequest,
    context: AuthContext = Depends(require_scope("write")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    item = _load_item(session, context, item_id)
    reject_review_item(session, item, context, reason=payload.reason)
    return {
        "review_item_id": item.id,
        "status": item.status,
        "reviewed_at": item.reviewed_at,
    }


__all__ = ["router"]
