"""对话入库 REST 入口；MCP 的 ``conversation_ingest`` 在 T13 复用本服务。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import ensure_workspace, resolve_workspace_id
from app.auth.session import AuthContext, require_scope
from app.db import get_db
from app.schemas.memory import ensure_no_nul
from app.services.extraction import ConversationIngestRequest, ingest_conversation

router = APIRouter(prefix="/api/v1/conversations", tags=["ingest"])


@router.post("/ingest", status_code=202)
def conversation_ingest(
    payload: ConversationIngestRequest,
    context: AuthContext = Depends(require_scope("write")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """登记一次对话抽取；相同 session + 消息返回同一 task_id。"""
    ensure_no_nul(payload.workspace_id, "workspace_id")
    resolved = resolve_workspace_id(context, payload.workspace_id)
    ensure_workspace(session, context, resolved)
    request = payload.model_copy(update={"workspace_id": resolved})
    result = ingest_conversation(session, request, context)
    return {
        "task_id": result.task_id,
        "candidate_count": result.candidate_count,
    }


__all__ = ["router"]
