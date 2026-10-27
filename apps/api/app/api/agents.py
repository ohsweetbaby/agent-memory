"""Agent 与 EndUser 管理：CRUD、软删除与跨 workspace 隔离。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    DEFAULT_PAGE_SIZE,
    ensure_workspace,
    paginate,
    resolve_workspace_id,
    workspace_filter,
)
from app.auth.session import AuthContext, require_scope
from app.db import get_db
from app.errors import ConflictError, NotFoundError
from app.models import Agent, EndUser
from app.models.base import utcnow

router = APIRouter(prefix="/api/v1", tags=["agents"])


class AgentCreate(BaseModel):
    workspace_id: str | None = Field(default=None, max_length=36)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)


class AgentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)


class EndUserCreate(BaseModel):
    workspace_id: str | None = Field(default=None, max_length=36)
    external_id: str = Field(min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)


def _agent_payload(agent: Agent) -> dict[str, Any]:
    return {
        "id": agent.id,
        "workspace_id": agent.workspace_id,
        "name": agent.name,
        "description": agent.description,
        "created_at": agent.created_at,
        "deleted_at": agent.deleted_at,
    }


def _end_user_payload(end_user: EndUser) -> dict[str, Any]:
    return {
        "id": end_user.id,
        "workspace_id": end_user.workspace_id,
        "external_id": end_user.external_id,
        "display_name": end_user.display_name,
        "created_at": end_user.created_at,
        "deleted_at": end_user.deleted_at,
    }


def _load_agent(session: Session, context: AuthContext, agent_id: str) -> Agent:
    """按 id 读取；跨 workspace 或已软删统一 404。"""
    agent = session.get(Agent, agent_id)
    if agent is None or agent.deleted_at is not None:
        raise NotFoundError("Agent 不存在")
    if context.workspace_id is not None and agent.workspace_id != context.workspace_id:
        raise NotFoundError("Agent 不存在")
    return agent


def _load_end_user(session: Session, context: AuthContext, end_user_id: str) -> EndUser:
    end_user = session.get(EndUser, end_user_id)
    if end_user is None or end_user.deleted_at is not None:
        raise NotFoundError("终端用户不存在")
    if context.workspace_id is not None and end_user.workspace_id != context.workspace_id:
        raise NotFoundError("终端用户不存在")
    return end_user


# ---- Agent ----


@router.get("/agents")
def list_agents(
    workspace_id: str | None = Query(default=None, max_length=36),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=DEFAULT_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    resolved = workspace_filter(context, workspace_id)
    statement = select(Agent)
    if resolved is not None:
        ensure_workspace(session, context, resolved)
        statement = statement.where(Agent.workspace_id == resolved)
    if not include_deleted:
        statement = statement.where(Agent.deleted_at.is_(None))
    items, next_cursor = paginate(session, statement, Agent, limit=limit, cursor=cursor)
    return {"items": [_agent_payload(item) for item in items], "next_cursor": next_cursor}


@router.post("/agents", status_code=201)
def create_agent(
    payload: AgentCreate,
    context: AuthContext = Depends(require_scope("write")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    resolved = resolve_workspace_id(context, payload.workspace_id)
    ensure_workspace(session, context, resolved)
    duplicate = session.scalar(
        select(Agent.id).where(
            Agent.workspace_id == resolved,
            Agent.name == payload.name,
            Agent.deleted_at.is_(None),
        )
    )
    if duplicate is not None:
        raise ConflictError("同名 Agent 已存在")

    agent = Agent(
        workspace_id=resolved, name=payload.name, description=payload.description
    )
    session.add(agent)
    try:
        session.commit()
    except IntegrityError as exc:  # 并发创建同名 Agent
        session.rollback()
        raise ConflictError("同名 Agent 已存在") from exc
    return _agent_payload(agent)


@router.get("/agents/{agent_id}")
def get_agent(
    agent_id: str,
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    return _agent_payload(_load_agent(session, context, agent_id))


@router.patch("/agents/{agent_id}")
def patch_agent(
    agent_id: str,
    payload: AgentPatch,
    context: AuthContext = Depends(require_scope("write")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    agent = _load_agent(session, context, agent_id)
    if payload.name is not None and payload.name != agent.name:
        duplicate = session.scalar(
            select(Agent.id).where(
                Agent.workspace_id == agent.workspace_id,
                Agent.name == payload.name,
                Agent.deleted_at.is_(None),
                Agent.id != agent.id,
            )
        )
        if duplicate is not None:
            raise ConflictError("同名 Agent 已存在")
        agent.name = payload.name
    if "description" in payload.model_fields_set:
        agent.description = payload.description
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("同名 Agent 已存在") from exc
    return _agent_payload(agent)


@router.delete("/agents/{agent_id}", status_code=204)
def delete_agent(
    agent_id: str,
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> Response:
    agent = _load_agent(session, context, agent_id)
    agent.deleted_at = utcnow()
    session.commit()
    return Response(status_code=204)


# ---- EndUser ----


@router.get("/end-users")
def list_end_users(
    workspace_id: str | None = Query(default=None, max_length=36),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=DEFAULT_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    resolved = workspace_filter(context, workspace_id)
    statement = select(EndUser)
    if resolved is not None:
        ensure_workspace(session, context, resolved)
        statement = statement.where(EndUser.workspace_id == resolved)
    if not include_deleted:
        statement = statement.where(EndUser.deleted_at.is_(None))
    items, next_cursor = paginate(session, statement, EndUser, limit=limit, cursor=cursor)
    return {
        "items": [_end_user_payload(item) for item in items],
        "next_cursor": next_cursor,
    }


@router.post("/end-users", status_code=201)
def create_end_user(
    payload: EndUserCreate,
    context: AuthContext = Depends(require_scope("write")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    resolved = resolve_workspace_id(context, payload.workspace_id)
    ensure_workspace(session, context, resolved)
    duplicate = session.scalar(
        select(EndUser.id).where(
            EndUser.workspace_id == resolved,
            EndUser.external_id == payload.external_id,
            EndUser.deleted_at.is_(None),
        )
    )
    if duplicate is not None:
        raise ConflictError("external_id 已存在")

    end_user = EndUser(
        workspace_id=resolved,
        external_id=payload.external_id,
        display_name=payload.display_name,
    )
    session.add(end_user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("external_id 已存在") from exc
    return _end_user_payload(end_user)


@router.delete("/end-users/{end_user_id}", status_code=204)
def delete_end_user(
    end_user_id: str,
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> Response:
    end_user = _load_end_user(session, context, end_user_id)
    end_user.deleted_at = utcnow()
    session.commit()
    return Response(status_code=204)
