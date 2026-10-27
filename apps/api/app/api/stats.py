"""管理台统计路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import ensure_workspace, workspace_filter
from app.auth.session import AuthContext, require_scope
from app.db import get_db
from app.services.stats import overview, timeseries

router = APIRouter(prefix="/api/v1/stats", tags=["stats"])


@router.get("/overview")
def stats_overview(
    workspace_id: str | None = Query(default=None, max_length=36),
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    resolved = workspace_filter(context, workspace_id)
    if resolved is not None:
        ensure_workspace(session, context, resolved)
    return overview(session, resolved).to_dict()


@router.get("/timeseries")
def stats_timeseries(
    workspace_id: str | None = Query(default=None, max_length=36),
    days: int = Query(default=30, ge=1, le=365),
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    resolved = workspace_filter(context, workspace_id)
    if resolved is not None:
        ensure_workspace(session, context, resolved)
    points = timeseries(session, resolved, days=days)
    return {
        "days": days,
        "items": [point.to_dict() for point in points],
    }


__all__ = ["router"]
