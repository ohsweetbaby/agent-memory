"""后台任务 REST 与 SSE：列出、触发巩固、回滚与进度转发（规格 §8/§11）。

``POST /jobs/run`` 在 M1 同步执行巩固服务（会话/夜间任务规模很小），但仍写入
``job_run`` 并把进度发布到 Redis pub/sub；SSE 只订阅该频道，客户端断开不会
影响任务。Celery 侧的定时任务复用同一服务入口。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from sse_starlette import EventSourceResponse

from app.api.deps import (
    DEFAULT_PAGE_SIZE,
    ensure_workspace,
    paginate,
    resolve_workspace_id,
    workspace_filter,
)
from app.auth.session import AuthContext, require_scope
from app.config import get_current_settings
from app.db import get_db
from app.errors import BadRequestError, NotFoundError
from app.models import JobRun
from app.models import Session as SessionModel
from app.schemas.memory import ensure_no_nul
from app.services.consolidation import (
    CONSOLIDATION_JOB_TYPES,
    JOB_TYPE_CONSOLIDATE_SESSION,
    consolidate_nightly,
    consolidate_session,
    job_channel,
    rollback_run,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

TERMINAL_STATUSES = frozenset({"completed", "failed", "dead", "rolled_back"})
DEFAULT_WINDOW = timedelta(hours=24)


class JobRunRequest(BaseModel):
    """``POST /jobs/run`` 的请求体；``params`` 由任务类型解释。"""

    type: Literal["consolidate_session", "consolidate_nightly"]
    params: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False


def _job_payload(job: JobRun) -> dict[str, Any]:
    return {
        "id": job.id,
        "type": job.type,
        "scope": job.scope,
        "status": job.status,
        "params": job.params or {},
        "result": job.result or {},
        "error": job.error,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "created_at": job.created_at,
    }


def _load_job(session: Session, context: AuthContext, job_id: str) -> JobRun:
    """跨 workspace 与不存在统一 404；任务通过 params.workspace_id 归属。"""
    ensure_no_nul(job_id, "id")
    job = session.get(JobRun, job_id)
    if job is None:
        raise NotFoundError("任务不存在")
    workspace_id = (job.params or {}).get("workspace_id")
    if context.workspace_id is not None and workspace_id != context.workspace_id:
        raise NotFoundError("任务不存在")
    return job


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_window(params: dict[str, Any]) -> tuple[datetime, datetime]:
    end = params.get("window_end")
    start = params.get("window_start")
    try:
        window_end = (
            _aware(datetime.fromisoformat(str(end)))
            if end
            else datetime.now(UTC)
        )
        window_start = (
            _aware(datetime.fromisoformat(str(start)))
            if start
            else window_end - DEFAULT_WINDOW
        )
    except ValueError as exc:
        raise BadRequestError("时间窗口无效", code="invalid_window") from exc
    return window_start, window_end


@router.get("")
def list_jobs(
    workspace_id: str | None = Query(default=None, max_length=36),
    job_type: str | None = Query(default=None, alias="type", max_length=64),
    status: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=DEFAULT_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    resolved = workspace_filter(context, workspace_id)
    statement = select(JobRun)
    if resolved is not None:
        ensure_workspace(session, context, resolved)
        statement = statement.where(
            JobRun.params["workspace_id"].astext == resolved
        )
    if job_type is not None:
        statement = statement.where(JobRun.type == job_type)
    if status is not None:
        statement = statement.where(JobRun.status == status)

    items, next_cursor = paginate(
        session, statement, JobRun, limit=limit, cursor=cursor
    )
    return {
        "items": [_job_payload(item) for item in items],
        "next_cursor": next_cursor,
    }


@router.get("/{job_id}")
def get_job(
    job_id: str,
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    return _job_payload(_load_job(session, context, job_id))


@router.post("/run")
def run_job(
    payload: JobRunRequest,
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """同步执行一次巩固；``dry_run`` 只返回差异，不创建 job_run。"""
    if payload.type not in CONSOLIDATION_JOB_TYPES:
        raise BadRequestError("不支持的任务类型", code="unsupported_job_type")

    if payload.type == JOB_TYPE_CONSOLIDATE_SESSION:
        session_id = str(payload.params.get("session_id") or "")
        ensure_no_nul(session_id, "session_id")
        if not session_id:
            raise BadRequestError("必须指定 session_id", code="invalid_request")
        conversation = session.get(SessionModel, session_id)
        if conversation is None:
            raise NotFoundError("会话不存在")
        ensure_workspace(session, context, conversation.workspace_id)
        diff = consolidate_session(
            session,
            session_id,
            dry_run=payload.dry_run,
            workspace_id=conversation.workspace_id,
            actor=context,
        )
        return diff.to_dict()

    requested = payload.params.get("workspace_id")
    resolved = resolve_workspace_id(
        context, str(requested) if requested else None
    )
    ensure_workspace(session, context, resolved)
    window_start, window_end = _parse_window(payload.params)
    diff = consolidate_nightly(
        session,
        window_start,
        window_end,
        scope=resolved,
        dry_run=payload.dry_run,
        actor=context,
    )
    return diff.to_dict()


@router.post("/{job_id}/rollback")
def rollback_job(
    job_id: str,
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_no_nul(job_id, "id")
    restored = rollback_run(
        session,
        job_id,
        workspace_id=context.workspace_id,
        actor=context,
    )
    return {"job_run_id": job_id, "restored": restored, "status": "rolled_back"}


@router.get("/{job_id}/events")
async def job_events(
    job_id: str,
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> EventSourceResponse:
    """把 Redis pub/sub 的任务进度转发为 SSE；断开连接不影响任务。"""
    job = _load_job(session, context, job_id)
    snapshot = _job_payload(job)

    async def event_stream():
        yield {
            "event": "snapshot",
            "data": json.dumps(snapshot, ensure_ascii=False, default=str),
        }
        if snapshot["status"] in TERMINAL_STATUSES:
            yield {
                "event": "done",
                "data": json.dumps(
                    {"job_id": job_id, "status": snapshot["status"]},
                    ensure_ascii=False,
                ),
            }
            return

        import redis.asyncio as aioredis

        client = aioredis.Redis.from_url(get_current_settings().redis_url)
        pubsub = client.pubsub()
        try:
            try:
                await pubsub.subscribe(job_channel(job_id))
            except Exception:
                logger.warning(
                    "job event subscription failed", extra={"job_id": job_id}
                )
                yield {
                    "event": "error",
                    "data": json.dumps(
                        {"job_id": job_id, "error": "redis_unavailable"},
                        ensure_ascii=False,
                    ),
                }
                return
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is None:
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                yield {"event": "progress", "data": str(data)}
                try:
                    status = json.loads(str(data)).get("status")
                except (TypeError, ValueError):
                    continue
                if status in TERMINAL_STATUSES:
                    yield {
                        "event": "done",
                        "data": json.dumps(
                            {"job_id": job_id, "status": status},
                            ensure_ascii=False,
                        ),
                    }
                    return
        except asyncio.CancelledError:  # 客户端断开
            raise
        finally:
            try:
                await pubsub.unsubscribe(job_channel(job_id))
                await pubsub.aclose()
            finally:
                await client.aclose()

    return EventSourceResponse(event_stream(), ping=15)


__all__ = ["router"]
