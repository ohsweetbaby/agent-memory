"""Outbox 分发、嵌入与索引投影任务。

设计要点：

- 写入路径只负责落 Postgres + outbox；这里的所有外部调用都可以失败重试，
  Postgres 始终是事实源。
- ``dispatch_outbox`` 用租约领取事件（``processing`` + 过期时间），投递后
  保持 ``processing``；只有 ``project_memory`` 完成全部投影后才置 ``done``。
  失败或 worker 崩溃时事件会在租约到期后重新投递。
- 投影按 ``projection_state(memory_id, target, version)`` 幂等；同一版本重复
  事件直接跳过，旧版本不会覆盖新版本。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import timedelta
from typing import Any, NoReturn

from celery.exceptions import Retry
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.adapters.neo4j import GraphEntity, GraphLink
from app.jobs.celery_app import celery_app
from app.jobs.runtime import Runtime, get_runtime
from app.models import (
    Entity,
    JobRun,
    Memory,
    MemoryEntity,
    MemoryLink,
    OutboxEvent,
    ProjectionState,
)
from app.models.base import utcnow
from app.repositories.embedding import EmbeddingRepository
from app.repositories.outbox import OutboxRepository
from app.services.audit import write_audit

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
LEASE_SECONDS = 300
#: 投影任务执行期间续租；Celery 8 次指数退避总时长小于该值。
PROJECTION_LEASE_SECONDS = 1800

TASK_DISPATCH_OUTBOX = "app.jobs.tasks.dispatch_outbox"
TASK_EMBED_MEMORY = "app.jobs.tasks.embed_memory"
TASK_PROJECT_MEMORY = "app.jobs.tasks.project_memory"
TASK_PROJECT_QDRANT = "app.jobs.tasks.project_qdrant"
TASK_PROJECT_NEO4J = "app.jobs.tasks.project_neo4j"
TASK_HEAL_PROJECTION_IDENTITY = "app.jobs.tasks.heal_projection_identity"
TASK_EXTRACT_CONVERSATION = "app.jobs.tasks.extract_conversation"
TASK_CONSOLIDATE_SESSION = "app.jobs.tasks.consolidate_session"
TASK_CONSOLIDATE_NIGHTLY = "app.jobs.tasks.consolidate_nightly"
TASK_CONSOLIDATE_IDLE = "app.jobs.tasks.consolidate_idle_sessions"

NEO4J_TARGET = "neo4j"

_REMOVED_STATUSES = frozenset({"deleted", "purged"})
_SUPPORTED_EVENTS = frozenset({"memory.upserted", "memory.deleted"})

#: eager 模式下模拟 broker 的进程内队列；生产环境直接 ``send_task``。
_PENDING: deque[tuple[str, tuple[Any, ...]]] = deque()

_TASK_OPTIONS: dict[str, Any] = {
    "autoretry_for": (Exception,),
    "max_retries": MAX_ATTEMPTS,
    "retry_backoff": 2,
    "retry_backoff_max": 300,
    "retry_jitter": True,
}

#: 抽取任务按规格 §11：最多重试 3 次、超时 10 分钟；不可重试的失败直接死信。
_EXTRACT_OPTIONS: dict[str, Any] = {
    "autoretry_for": (Exception,),
    "max_retries": 3,
    "retry_backoff": 2,
    "retry_backoff_max": 60,
    "retry_jitter": True,
    "time_limit": 600,
}

#: 巩固任务与抽取同样最多重试 3 次、超时 10 分钟。
_CONSOLIDATE_OPTIONS: dict[str, Any] = dict(_EXTRACT_OPTIONS)


def enqueue(task_name: str, *args: Any) -> None:
    """投递任务；eager 模式只入内存队列，等待 :func:`run_worker_once`。"""
    runtime = get_runtime()
    if runtime.eager:
        _PENDING.append((task_name, args))
        return
    celery_app.send_task(task_name, args=list(args))


def run_worker_once(limit: int = 1000) -> int:
    """开发/测试用的单轮 worker：按入队顺序执行内存队列中的任务。"""
    processed = 0
    while _PENDING and processed < limit:
        task_name, args = _PENDING.popleft()
        task = celery_app.tasks[task_name]
        task.run(*args)
        processed += 1
    return processed


def reset_pending() -> None:
    _PENDING.clear()


def _retry_or_reraise(task: Any, exc: Exception) -> NoReturn:
    """直接调用 ``task.run`` 时抛 :class:`Retry`，worker 内交给 autoretry。"""
    if task.request.called_directly:
        raise Retry(exc=exc) from exc
    raise exc


def _is_removed(memory: Memory) -> bool:
    return memory.status in _REMOVED_STATUSES or memory.deleted_at is not None


def qdrant_target(qdrant: Any) -> str:
    """Qdrant 的幂等键包含 collection 身份，换模型/维度后自动回填。"""
    return f"qdrant:{qdrant.collection}"


def _projected_version(db: Session, memory_id: str, target: str) -> int:
    value = db.scalar(
        select(ProjectionState.version).where(
            ProjectionState.memory_id == memory_id,
            ProjectionState.target == target,
        )
    )
    return int(value or 0)


def mark_projected(db: Session, memory_id: str, target: str, version: int) -> None:
    """只在版本前进时更新投影状态；旧事件不会回退状态。"""
    statement = pg_insert(ProjectionState).values(
        memory_id=memory_id,
        target=target,
        version=version,
        projected_at=utcnow(),
        error=None,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[ProjectionState.memory_id, ProjectionState.target],
        set_={
            "version": statement.excluded.version,
            "projected_at": statement.excluded.projected_at,
            "error": None,
        },
        where=ProjectionState.version < statement.excluded.version,
    )
    db.execute(statement)


def _entity_refs(db: Session, memory_id: str) -> list[GraphEntity]:
    rows = db.execute(
        select(Entity, MemoryEntity.role)
        .join(MemoryEntity, MemoryEntity.entity_id == Entity.id)
        .where(MemoryEntity.memory_id == memory_id)
        .order_by(Entity.id)
    ).all()
    return [
        GraphEntity(id=entity.id, name=entity.name, type=entity.type, role=role)
        for entity, role in rows
    ]


def _entity_ids(db: Session, memory_id: str) -> list[str]:
    return [
        str(value)
        for value in db.scalars(
            select(MemoryEntity.entity_id)
            .where(MemoryEntity.memory_id == memory_id)
            .order_by(MemoryEntity.entity_id)
        )
    ]


def _links(db: Session, memory_id: str) -> list[GraphLink]:
    rows = db.scalars(
        select(MemoryLink)
        .where(MemoryLink.from_id == memory_id)
        .order_by(MemoryLink.id)
    )
    return [
        GraphLink(
            from_id=link.from_id,
            to_id=link.to_id,
            relation=link.relation,
            weight=link.weight,
            evidence=link.evidence,
        )
        for link in rows
    ]


def _qdrant_payload(memory: Memory, entity_ids: list[str]) -> dict[str, Any]:
    return {
        "workspace_id": memory.workspace_id,
        "scope": memory.scope,
        "scope_id": memory.scope_id,
        "type": memory.type,
        "status": memory.status,
        "importance": float(memory.importance or 0.0),
        "valid_to": memory.valid_to,
        "last_reinforced_at": memory.last_reinforced_at,
        "entity_ids": entity_ids,
        "version": memory.version,
    }


def _ensure_embedding(
    runtime: Runtime, db: Session, memory: Memory, *, force: bool = False
) -> list[float]:
    repo = EmbeddingRepository(db)
    identity = runtime.embedding.identity
    vector = None if force else repo.get(memory.id, identity)
    if vector is not None and len(vector) != runtime.embedding.dim:
        # 模型名复用但维度已变更时，旧缓存不可用。
        vector = None
    if vector is None:
        generated = asyncio.run(runtime.embedding.embed([memory.content]))[0]
        repo.upsert(
            memory.id,
            identity,
            runtime.embedding.dim,
            generated,
        )
        vector = generated
    return vector


def project_qdrant_record(
    db: Session,
    memory: Memory,
    version: int | None = None,
    *,
    force: bool = False,
    refresh_embedding: bool = False,
) -> bool:
    """把一条记忆投影到 Qdrant；返回是否实际执行了写入。

    ``force`` 同时用于重建（跳过版本检查并重算嵌入）；普通投影只在嵌入
    缺失或身份不匹配时补齐。
    """
    runtime = get_runtime()
    target = qdrant_target(runtime.qdrant)
    target_version = int(version or memory.version)
    if not force and _projected_version(db, memory.id, target) >= target_version:
        return False

    if _is_removed(memory):
        runtime.qdrant.delete(memory.id)
        mark_projected(db, memory.id, target, target_version)
        logger.info(
            "memory removed from qdrant",
            extra={"memory_id": memory.id},
        )
        return True

    vector = _ensure_embedding(
        runtime, db, memory, force=force or refresh_embedding
    )
    entity_ids = _entity_ids(db, memory.id)
    runtime.qdrant.upsert(memory.id, vector, _qdrant_payload(memory, entity_ids))
    mark_projected(db, memory.id, target, target_version)
    logger.info(
        "memory projected to qdrant",
        extra={
            "memory_id": memory.id,
            "dim": len(vector),
            "entity_count": len(entity_ids),
        },
    )
    return True


def project_neo4j_record(
    db: Session,
    memory: Memory,
    version: int | None = None,
    *,
    force: bool = False,
) -> bool:
    """把一条记忆投影到 Neo4j；返回是否实际执行了写入。"""
    runtime = get_runtime()
    if runtime.neo4j is None:
        logger.warning("neo4j is not configured; projection skipped")
        return False

    target_version = int(version or memory.version)
    if not force and _projected_version(db, memory.id, NEO4J_TARGET) >= target_version:
        return False

    if _is_removed(memory):
        runtime.neo4j.delete(memory.id)
        mark_projected(db, memory.id, NEO4J_TARGET, target_version)
        logger.info(
            "memory removed from neo4j",
            extra={"memory_id": memory.id},
        )
        return True

    entities = _entity_refs(db, memory.id)
    links = _links(db, memory.id)
    runtime.neo4j.upsert_memory(memory, entities, links)
    mark_projected(db, memory.id, NEO4J_TARGET, target_version)
    return True


def _record_projection_error(
    memory_id: str, target: str, exc: Exception
) -> None:
    """投影失败时单独提交错误，避免随事务回滚而丢失。

    新建行固定写 ``version=0``：错误行只用于诊断，不能冒充已投影版本；
    已有状态行则只更新 ``error``，保留其版本。
    """
    runtime = get_runtime()
    message = (str(exc) or exc.__class__.__name__)[:2000]
    try:
        with runtime.session_scope() as db:
            statement = pg_insert(ProjectionState).values(
                memory_id=memory_id,
                target=target,
                version=0,
                projected_at=utcnow(),
                error=message,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[ProjectionState.memory_id, ProjectionState.target],
                set_={"error": message},
            )
            db.execute(statement)
            db.commit()
    except Exception:  # 记忆已被物理删除时 FK 会失败；不能影响重试
        logger.exception(
            "failed to record projection error",
            extra={"memory_id": memory_id, "target": target},
        )


def _touch_event_lease(
    runtime: Runtime, event_id: str | None, seconds: int = PROJECTION_LEASE_SECONDS
) -> None:
    """投影执行期间续租，避免长重试链被其他 worker 重复领取。"""
    if not event_id:
        return
    with runtime.session_scope() as db:
        db.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.status == "processing",
            )
            .values(next_attempt_at=utcnow() + timedelta(seconds=seconds))
        )
        db.commit()


def _mark_event_done(runtime: Runtime, event_id: str | None) -> None:
    if not event_id:
        return
    with runtime.session_scope() as db:
        result = db.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.status == "processing",
            )
            .values(status="done", next_attempt_at=utcnow())
        )
        db.commit()
        if not result.rowcount:
            logger.warning(
                "outbox event not marked done",
                extra={"event_id": event_id},
            )


def _mark_event_failed(
    runtime: Runtime, event_id: str | None, exc: Exception, *, dead: bool
) -> None:
    if not event_id:
        return
    message = (str(exc) or exc.__class__.__name__)[:2000]
    with runtime.session_scope() as db:
        event = db.get(OutboxEvent, event_id)
        if event is None or event.status in {"done", "dead"}:
            return
        payload = dict(event.payload or {})
        payload["last_error"] = message
        payload["failed_at"] = utcnow().isoformat()
        event.payload = payload
        if dead:
            event.status = "dead"
            event.next_attempt_at = utcnow()
        else:
            event.next_attempt_at = utcnow() + timedelta(
                seconds=PROJECTION_LEASE_SECONDS
            )
        db.commit()
    logger.warning(
        "outbox projection failed",
        extra={"event_id": event_id, "dead": dead},
    )


def _run_qdrant_step(runtime: Runtime, memory_id: str, version: int | None) -> None:
    with runtime.session_scope() as db:
        memory = db.get(Memory, memory_id)
        if memory is None:
            runtime.qdrant.delete(memory_id)
        else:
            project_qdrant_record(
                db, memory, version, refresh_embedding=True
            )
        db.commit()


def _run_neo4j_step(runtime: Runtime, memory_id: str, version: int | None) -> None:
    if runtime.neo4j is None:
        return
    with runtime.session_scope() as db:
        memory = db.get(Memory, memory_id)
        if memory is None:
            runtime.neo4j.delete(memory_id)
        else:
            project_neo4j_record(db, memory, version)
        db.commit()


def _project_one(runtime: Runtime, memory_id: str, version: int | None) -> None:
    """依次投影 Qdrant / Neo4j；任一步失败都会留下可查询的 error。"""
    qtarget = qdrant_target(runtime.qdrant)
    try:
        _run_qdrant_step(runtime, memory_id, version)
    except Exception as exc:
        _record_projection_error(memory_id, qtarget, exc)
        raise
    try:
        _run_neo4j_step(runtime, memory_id, version)
    except Exception as exc:
        _record_projection_error(memory_id, NEO4J_TARGET, exc)
        raise


def _dispatch(limit: int) -> int:
    runtime = get_runtime()
    with runtime.session_scope() as db:
        repo = OutboxRepository(db)
        dead = repo.dead_letter_exhausted(max_attempts=MAX_ATTEMPTS)
        if dead:
            logger.warning("outbox events moved to dead letter", extra={"count": dead})
        events = repo.claim_batch(
            limit=limit,
            lease_seconds=LEASE_SECONDS,
            max_attempts=MAX_ATTEMPTS,
        )
        db.commit()

        dispatched = 0
        for event in events:
            payload = event.payload or {}
            memory_id = str(payload.get("memory_id") or event.aggregate_id)
            version = payload.get("version")
            try:
                if event.event_type not in _SUPPORTED_EVENTS:
                    logger.warning(
                        "unknown outbox event ignored",
                        extra={
                            "event_id": event.id,
                            "event_type": event.event_type,
                        },
                    )
                    event.status = "done"
                    continue
                # 单一编排任务负责“嵌入 → 两个投影 → 事件 done”，
                # 只有投影真正成功才会把 outbox 置为 done。
                enqueue(TASK_PROJECT_MEMORY, event.id, memory_id, version)
            except Exception:
                # 保留 processing，租约到期后由其他 worker 重新投递。
                logger.exception(
                    "outbox dispatch failed",
                    extra={"event_id": event.id, "memory_id": memory_id},
                )
                continue
            dispatched += 1

        db.commit()
        return dispatched


@celery_app.task(bind=True, name=TASK_PROJECT_MEMORY, **_TASK_OPTIONS)
def project_memory(
    self: Any, event_id: str | None, memory_id: str, version: int | None = None
) -> bool:
    """投影一条记忆的完整链路；成功后才把 outbox 事件置为 done。"""
    runtime = get_runtime()
    try:
        _touch_event_lease(runtime, event_id)
        _project_one(runtime, memory_id, version)
        _mark_event_done(runtime, event_id)
        return True
    except Retry:
        raise
    except Exception as exc:
        final = self.request.retries >= (self.max_retries or 0)
        _mark_event_failed(runtime, event_id, exc, dead=final)
        if self.request.called_directly:
            raise Retry(exc=exc) from exc
        raise


@celery_app.task(bind=True, name=TASK_DISPATCH_OUTBOX, **_TASK_OPTIONS)
def dispatch_outbox(self: Any, limit: int = 50) -> int:
    """领取 pending/stale 事件并投递投影任务。"""
    try:
        return _dispatch(limit)
    except Retry:
        raise
    except Exception as exc:
        _retry_or_reraise(self, exc)


def _projection_identity_gaps(
    db: Session, target: str, limit: int
) -> list[tuple[str, int]]:
    """找出当前 collection 缺少最新投影状态的 active 记忆。"""
    statement = (
        select(Memory.id, Memory.version)
        .outerjoin(
            ProjectionState,
            and_(
                ProjectionState.memory_id == Memory.id,
                ProjectionState.target == target,
            ),
        )
        .where(
            Memory.status == "active",
            Memory.deleted_at.is_(None),
            or_(
                ProjectionState.id.is_(None),
                ProjectionState.version < Memory.version,
            ),
        )
        .order_by(Memory.id)
        .limit(limit)
    )
    return [(row[0], int(row[1])) for row in db.execute(statement).all()]


@celery_app.task(bind=True, name=TASK_HEAL_PROJECTION_IDENTITY, **_TASK_OPTIONS)
def heal_projection_identity(self: Any, limit: int = 500) -> int:
    """配置切换自愈：为当前 collection 缺失投影状态的记忆补投任务。

    beat 每 10 分钟调用一次。只处理 active、未删除且状态版本落后于记忆版本
    的条目；重试失败的记录版本为 0，下一轮会再次被拾取。
    """
    try:
        runtime = get_runtime()
        target = qdrant_target(runtime.qdrant)
        with runtime.session_scope() as db:
            rows = _projection_identity_gaps(db, target, limit)
        if rows:
            logger.warning(
                "projection identity drift detected",
                extra={"target": target, "count": len(rows)},
            )
        for memory_id, version in rows:
            enqueue(TASK_PROJECT_MEMORY, None, memory_id, version)
        return len(rows)
    except Retry:
        raise
    except Exception as exc:
        _retry_or_reraise(self, exc)


@celery_app.task(bind=True, name=TASK_EMBED_MEMORY, **_TASK_OPTIONS)
def embed_memory(self: Any, memory_id: str, version: int | None = None) -> bool:
    """生成/刷新 ``memory_embedding``；不直接访问 Qdrant。"""
    try:
        runtime = get_runtime()
        with runtime.session_scope() as db:
            memory = db.get(Memory, memory_id)
            if memory is None or _is_removed(memory):
                return False
            _ensure_embedding(runtime, db, memory, force=True)
            db.commit()
            logger.info(
                "memory embedded",
                extra={"memory_id": memory.id, "chars": len(memory.content)},
            )
            return True
    except Retry:
        raise
    except Exception as exc:
        _retry_or_reraise(self, exc)


@celery_app.task(bind=True, name=TASK_PROJECT_QDRANT, **_TASK_OPTIONS)
def project_qdrant(
    self: Any, memory_id: str, version: int | None = None
) -> bool:
    """按版本幂等写入 Qdrant；缺嵌入时先补齐。"""
    try:
        runtime = get_runtime()
        with runtime.session_scope() as db:
            memory = db.get(Memory, memory_id)
            if memory is None:
                runtime.qdrant.delete(memory_id)
                return False
            changed = project_qdrant_record(db, memory, version)
            db.commit()
            return changed
    except Retry:
        raise
    except Exception as exc:
        runtime = get_runtime()
        _record_projection_error(
            memory_id,
            qdrant_target(runtime.qdrant),
            exc,
        )
        _retry_or_reraise(self, exc)


@celery_app.task(bind=True, name=TASK_PROJECT_NEO4J, **_TASK_OPTIONS)
def project_neo4j(
    self: Any, memory_id: str, version: int | None = None
) -> bool:
    """按版本幂等写入 Neo4j；MERGE 保证重复事件安全。"""
    try:
        runtime = get_runtime()
        with runtime.session_scope() as db:
            memory = db.get(Memory, memory_id)
            if memory is None:
                if runtime.neo4j is not None:
                    runtime.neo4j.delete(memory_id)
                return False
            changed = project_neo4j_record(db, memory, version)
            db.commit()
            return changed
    except Retry:
        raise
    except Exception as exc:
        _record_projection_error(memory_id, NEO4J_TARGET, exc)
        _retry_or_reraise(self, exc)


@celery_app.task(bind=True, name=TASK_EXTRACT_CONVERSATION, **_EXTRACT_OPTIONS)
def extract_conversation(self: Any, job_id: str) -> dict[str, Any] | None:
    """执行对话抽取；不可解析的 LLM 输出直接进入死信并写审计。"""
    from app.services.extraction import (
        ExtractionError,
        mark_job_dead,
        process_extraction_job,
    )

    runtime = get_runtime()
    try:
        with runtime.session_scope() as db:
            job = db.get(JobRun, job_id)
            if job is None:
                logger.warning("extraction job missing", extra={"job_id": job_id})
                return None
            if job.status in {"completed", "dead"}:
                return dict(job.result or {})
            return dict(process_extraction_job(db, job))
    except ExtractionError as exc:
        with runtime.session_scope() as db:
            mark_job_dead(db, job_id, exc)
        return None
    except Retry:
        raise
    except Exception as exc:
        final = self.request.retries >= (self.max_retries or 0)
        if final:
            with runtime.session_scope() as db:
                mark_job_dead(db, job_id, exc)
            raise
        _retry_or_reraise(self, exc)


@celery_app.task(bind=True, name=TASK_CONSOLIDATE_SESSION, **_CONSOLIDATE_OPTIONS)
def consolidate_session(self: Any, session_id: str) -> dict[str, Any] | None:
    """会话结束/空闲触发的巩固任务；失败保留 job_run.error 并可重试。"""
    from app.services import consolidation as service

    runtime = get_runtime()
    try:
        with runtime.session_scope() as db:
            return service.consolidate_session(db, session_id).to_dict()
    except Retry:
        raise
    except Exception as exc:
        try:
            with runtime.session_scope() as db:
                job_id = service.session_job_id(db, session_id)
                if job_id is not None:
                    service.mark_job_failed(db, job_id, exc)
        except Exception:  # 标记失败不能掩盖原始错误
            logger.exception("failed to mark consolidation job failed")
        _retry_or_reraise(self, exc)


@celery_app.task(bind=True, name=TASK_CONSOLIDATE_NIGHTLY, **_CONSOLIDATE_OPTIONS)
def consolidate_nightly(
    self: Any,
    window_start: str | None = None,
    window_end: str | None = None,
    scope: str | None = None,
) -> dict[str, Any] | None:
    """beat 每日 03:00 触发；默认窗口为过去 24 小时。"""
    from datetime import datetime, timedelta

    from app.models.base import utcnow
    from app.services import consolidation as service

    end = datetime.fromisoformat(window_end) if window_end else utcnow()
    start = (
        datetime.fromisoformat(window_start)
        if window_start
        else end - timedelta(hours=24)
    )
    runtime = get_runtime()
    try:
        with runtime.session_scope() as db:
            return service.consolidate_nightly(
                db, start, end, scope=scope
            ).to_dict()
    except Retry:
        raise
    except Exception as exc:
        try:
            with runtime.session_scope() as db:
                job_id = service.job_id_for(
                    service.JOB_TYPE_CONSOLIDATE_NIGHTLY,
                    start,
                    end,
                    scope or "all",
                )
                service.mark_job_failed(db, job_id, exc)
        except Exception:  # 标记失败不能掩盖原始错误
            logger.exception("failed to mark consolidation job failed")
        _retry_or_reraise(self, exc)


@celery_app.task(bind=True, name=TASK_CONSOLIDATE_IDLE, **_CONSOLIDATE_OPTIONS)
def consolidate_idle_sessions(self: Any, limit: int = 50) -> int:
    """扫描已结束或空闲 ≥ 30 分钟的会话，逐个投递会话巩固。"""
    from app.services import consolidation as service

    runtime = get_runtime()
    processed = 0
    try:
        with runtime.session_scope() as db:
            session_ids = service.find_sessions_to_consolidate(db, limit=limit)
            for session_id in session_ids:
                enqueue(TASK_CONSOLIDATE_SESSION, session_id)
                processed += 1
        return processed
    except Retry:
        raise
    except Exception as exc:
        _retry_or_reraise(self, exc)


__all__ = [
    "consolidate_idle_sessions",
    "consolidate_nightly",
    "consolidate_session",
    "dispatch_outbox",
    "embed_memory",
    "enqueue",
    "extract_conversation",
    "heal_projection_identity",
    "mark_projected",
    "project_memory",
    "project_neo4j",
    "project_neo4j_record",
    "project_qdrant",
    "project_qdrant_record",
    "qdrant_target",
    "reset_pending",
    "run_worker_once",
]
