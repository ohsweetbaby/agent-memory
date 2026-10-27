"""API 路由聚合。"""

from __future__ import annotations

from app.api import agents, auth, keys, workspaces

ROUTERS = (auth.router, workspaces.router, agents.router, keys.router)

__all__ = ["ROUTERS"]
