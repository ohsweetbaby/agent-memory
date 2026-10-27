"""API 路由聚合。"""

from __future__ import annotations

from app.api import agents, auth, keys, memories, search, stats, workspaces

ROUTERS = (
    auth.router,
    workspaces.router,
    agents.router,
    keys.router,
    memories.router,
    search.router,
    search.timeline_router,
    stats.router,
)

__all__ = ["ROUTERS"]
