"""检索 REST 路由：``POST /api/v1/search``。

路由只负责认证、workspace 校验与依赖装配；四路召回与融合全部在
:mod:`app.services.search` 中完成，便于 MCP 层复用同一服务。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.adapters.llm import build_llm_provider, build_reranker
from app.api.deps import ensure_workspace, resolve_workspace_id
from app.auth.session import AuthContext, require_scope
from app.config import get_current_settings
from app.db import get_db
from app.jobs.runtime import Runtime, get_runtime
from app.schemas.search import SearchRequest, SearchResult
from app.services.search import search_memories

router = APIRouter(prefix="/api/v1/search", tags=["search"])


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


__all__ = ["router", "search_runtime"]
