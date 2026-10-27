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
    audit_actor,
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
from app.services.audit import write_audit

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


class EndUserPatch(BaseModel):
    external_id: str | None = Field(default=None, min_length=1, max_length=255)
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


def _agent_snapshot(agent: Agent) -> dict[str, Any]:
    return {"name": agent.name, "description": agent.description}


def _end_user_snapshot(end_user: EndUser) -> dict[str, Any]:
    return {"external_id": end_user.external_id, "display_name": end_user.display_name}


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
    actor_type, actor_id = audit_actor(context)
    try:
        session.add(agent)
        session.flush()  # 唯一索引冲突必须发生在 try 内，才能转成 409
        write_audit(
            session,
            action="agent.created",
            target_type="agent",
            target_id=agent.id,
            workspace_id=resolved,
            actor_type=actor_type,
            actor_id=actor_id,
            after=_agent_snapshot(agent),
        )
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
    before = _agent_snapshot(agent)
    actor_type, actor_id = audit_actor(context)
    try:
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
        # write_audit 内部 flush 会先刷出上面的 UPDATE，必须也在 try 内。
        write_audit(
            session,
            action="agent.updated",
            target_type="agent",
            target_id=agent.id,
            workspace_id=agent.workspace_id,
            actor_type=actor_type,
            actor_id=actor_id,
            before=before,
            after=_agent_snapshot(agent),
        )
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
    before = _agent_snapshot(agent)
    agent.deleted_at = utcnow()
    actor_type, actor_id = audit_actor(context)
    write_audit(
        session,
        action="agent.deleted",
        target_type="agent",
        target_id=agent.id,
        workspace_id=agent.workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        before=before,
        after={"deleted_at": agent.deleted_at.isoformat()},
    )
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
    actor_type, actor_id = audit_actor(context)
    try:
        session.add(end_user)
        session.flush()  # 唯一索引冲突必须发生在 try 内，才能转成 409
        write_audit(
            session,
            action="end_user.created",
            target_type="end_user",
            target_id=end_user.id,
            workspace_id=resolved,
            actor_type=actor_type,
            actor_id=actor_id,
            after=_end_user_snapshot(end_user),
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("external_id 已存在") from exc
    return _end_user_payload(end_user)


@router.patch("/end-users/{end_user_id}")
def patch_end_user(
    end_user_id: str,
    payload: EndUserPatch,
    context: AuthContext = Depends(require_scope("write")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    end_user = _load_end_user(session, context, end_user_id)
    before = _end_user_snapshot(end_user)
    actor_type, actor_id = audit_actor(context)
    try:
        if (
            payload.external_id is not None
            and payload.external_id != end_user.external_id
        ):
            duplicate = session.scalar(
                select(EndUser.id).where(
                    EndUser.workspace_id == end_user.workspace_id,
                    EndUser.external_id == payload.external_id,
                    EndUser.deleted_at.is_(None),
                    EndUser.id != end_user.id,
                )
            )
            if duplicate is not None:
                raise ConflictError("external_id 已存在")
            end_user.external_id = payload.external_id
        if "display_name" in payload.model_fields_set:
            end_user.display_name = payload.display_name
        # write_audit 内部 flush 会先刷出上面的 UPDATE，必须也在 try 内。
        write_audit(
            session,
            action="end_user.updated",
            target_type="end_user",
            target_id=end_user.id,
            workspace_id=end_user.workspace_id,
            actor_type=actor_type,
            actor_id=actor_id,
            before=before,
            after=_end_user_snapshot(end_user),
        )
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
    before = _end_user_snapshot(end_user)
    end_user.deleted_at = utcnow()
    actor_type, actor_id = audit_actor(context)
    write_audit(
        session,
        action="end_user.deleted",
        target_type="end_user",
        target_id=end_user.id,
        workspace_id=end_user.workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        before=before,
        after={"deleted_at": end_user.deleted_at.isoformat()},
    )
    session.commit()
    return Response(status_code=204)
