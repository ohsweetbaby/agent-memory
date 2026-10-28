"""工作区管理：列表、创建、详情、重命名。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import DEFAULT_PAGE_SIZE, audit_actor, ensure_workspace, paginate
from app.auth.session import AuthContext, require_scope, require_session_scope
from app.db import get_db
from app.models import Workspace
from app.services.audit import write_audit

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class WorkspacePatch(BaseModel):
    name: str = Field(min_length=1, max_length=255)


def _workspace_payload(workspace: Workspace) -> dict[str, Any]:
    return {
        "id": workspace.id,
        "name": workspace.name,
        "created_at": workspace.created_at,
    }


@router.get("")
def list_workspaces(
    limit: int = Query(default=DEFAULT_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    statement = select(Workspace)
    if context.workspace_id is not None:
        # API Key 只能看到自己的 workspace。
        statement = statement.where(Workspace.id == context.workspace_id)
    items, next_cursor = paginate(
        session, statement, Workspace, limit=limit, cursor=cursor
    )
    return {"items": [_workspace_payload(item) for item in items], "next_cursor": next_cursor}


@router.post("", status_code=201)
def create_workspace(
    payload: WorkspaceCreate,
    context: AuthContext = Depends(require_session_scope("admin")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = Workspace(name=payload.name)
    session.add(workspace)
    session.flush()
    actor_type, actor_id = audit_actor(context)
    write_audit(
        session,
        action="workspace.created",
        target_type="workspace",
        target_id=workspace.id,
        workspace_id=workspace.id,
        actor_type=actor_type,
        actor_id=actor_id,
        after={"name": workspace.name},
    )
    session.commit()
    return _workspace_payload(workspace)


@router.get("/{workspace_id}")
def get_workspace(
    workspace_id: str,
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    return _workspace_payload(ensure_workspace(session, context, workspace_id))


@router.patch("/{workspace_id}")
def patch_workspace(
    workspace_id: str,
    payload: WorkspacePatch,
    context: AuthContext = Depends(require_session_scope("admin")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = ensure_workspace(session, context, workspace_id)
    previous_name = workspace.name
    workspace.name = payload.name
    actor_type, actor_id = audit_actor(context)
    write_audit(
        session,
        action="workspace.updated",
        target_type="workspace",
        target_id=workspace.id,
        workspace_id=workspace.id,
        actor_type=actor_type,
        actor_id=actor_id,
        before={"name": previous_name},
        after={"name": workspace.name},
    )
    session.commit()
    return _workspace_payload(workspace)
