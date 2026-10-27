"""检索 REST 路由：``POST /api/v1/search``。

路由只负责认证、workspace 校验与依赖装配；四路召回与融合全部在
:mod:`app.services.search` 中完成，便于 MCP 层复用同一服务。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.llm import build_llm_provider, build_reranker
from app.api.deps import ensure_workspace, resolve_workspace_id, workspace_filter
from app.auth.session import AuthContext, require_scope
from app.config import get_current_settings
from app.db import get_db
from app.jobs.runtime import Runtime, get_runtime
from app.models import Entity, Episode, Memory, MemoryEntity
from app.models import Session as SessionModel
from app.schemas.search import SearchRequest, SearchResult
from app.services.search import search_memories

router = APIRouter(prefix="/api/v1/search", tags=["search"])
timeline_router = APIRouter(prefix="/api/v1", tags=["timeline"])


def search_runtime() -> Runtime:
    """检索所需的嵌入、Qdrant 与 Neo4j 依赖；测试可覆盖该依赖。"""
    return get_runtime()


@router.post("")
async def search(
    payload: SearchRequest,
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
    runtime: Runtime = Depends(search_runtime),
) -> SearchResult:
    """四路召回 + RRF 融合；单通道失败只降级，不整体失败。"""
    workspace_id = resolve_workspace_id(context, payload.workspace_id)
    ensure_workspace(session, context, workspace_id)

    request = payload.model_copy(update={"workspace_id": workspace_id})
    settings = get_current_settings()
    llm = (
        build_llm_provider(settings)
        if request.mode == "deep" and settings.search_query_rewrite
        else None
    )
    return await search_memories(
        session,
        request,
        runtime.embedding,
        runtime.qdrant,
        runtime.neo4j,
        llm=llm,
        reranker=build_reranker(settings),
    )


def _aware(value: datetime) -> datetime:
    """查询参数可能不带时区；统一按 UTC 比较。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@timeline_router.get("/timeline")
def get_timeline(
    session_id: str | None = Query(default=None, max_length=36),
    entity: str | None = Query(default=None, max_length=255),
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    workspace_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=100, ge=1, le=500),
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """合并 episode 与 session 作用域记忆，按时间升序返回。"""
    resolved = workspace_filter(context, workspace_id)
    if resolved is not None:
        ensure_workspace(session, context, resolved)

    memory_statement = select(Memory).where(
        Memory.deleted_at.is_(None),
        Memory.status != "deleted",
        Memory.scope == "session",
    )
    if resolved is not None:
        memory_statement = memory_statement.where(
            Memory.workspace_id == resolved
        )
    if session_id is not None:
        memory_statement = memory_statement.where(
            Memory.scope_id == session_id
        )
    if entity is not None:
        entity_memory_ids = (
            select(MemoryEntity.memory_id)
            .join(Entity, MemoryEntity.entity_id == Entity.id)
            .where(Entity.name == entity)
        )
        memory_statement = memory_statement.where(
            Memory.id.in_(entity_memory_ids)
        )

    items: list[dict[str, Any]] = []
    for memory in session.scalars(memory_statement):
        timestamp = _aware(memory.valid_from or memory.created_at)
        items.append(
            {
                "id": memory.id,
                "kind": "memory",
                "timestamp": timestamp,
                "workspace_id": memory.workspace_id,
                "type": memory.type,
                "scope": memory.scope,
                "scope_id": memory.scope_id,
                "content": memory.content,
                "summary": memory.summary,
                "status": memory.status,
                "importance": memory.importance,
                "metadata": memory.metadata_ or {},
            }
        )

    # entity 过滤只对记忆生效；episode 没有实体关联，直接跳过。
    if entity is None:
        episode_statement = select(Episode, SessionModel.workspace_id).join(
            SessionModel, Episode.session_id == SessionModel.id
        )
        if resolved is not None:
            episode_statement = episode_statement.where(
                SessionModel.workspace_id == resolved
            )
        if session_id is not None:
            episode_statement = episode_statement.where(
                Episode.session_id == session_id
            )
        for episode, episode_workspace_id in session.execute(
            episode_statement
        ).all():
            timestamp = _aware(episode.started_at or episode.created_at)
            items.append(
                {
                    "id": episode.id,
                    "kind": "episode",
                    "timestamp": timestamp,
                    "workspace_id": episode_workspace_id,
                    "session_id": episode.session_id,
                    "title": episode.title,
                    "summary": episode.summary,
                    "outcome": episode.outcome,
                    "started_at": episode.started_at,
                    "ended_at": episode.ended_at,
                }
            )

    if start is not None:
        start_at = _aware(start)
        items = [item for item in items if item["timestamp"] >= start_at]
    if end is not None:
        end_at = _aware(end)
        items = [item for item in items if item["timestamp"] <= end_at]

    items.sort(key=lambda item: (item["timestamp"], item["kind"], item["id"]))
    return {"items": items[:limit]}


__all__ = ["router", "search_runtime", "timeline_router"]
