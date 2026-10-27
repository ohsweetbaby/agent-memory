"""API 路由聚合。"""

from __future__ import annotations

from app.api import (
    agents,
    auth,
    export,
    ingest,
    jobs,
    keys,
    memories,
    review,
    search,
    stats,
    workspaces,
)

ROUTERS = (
    auth.router,
    workspaces.router,
    agents.router,
    keys.router,
    memories.router,
    search.router,
    search.timeline_router,
    stats.router,
    review.router,
    ingest.router,
    jobs.router,
    export.router,
)

__all__ = ["ROUTERS"]
