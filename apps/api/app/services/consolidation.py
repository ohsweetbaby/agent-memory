"""记忆巩固：会话结束/夜间批量、dry-run、幂等与回滚（规格 §5.5/§11）。

一次巩固运行对应一条 ``job_run``，其创建/修改的记忆都写
``metadata.consolidation_run_id`` 与同名列；回滚按该 run 反向操作：

- 新增记忆：软删除并移除 run 标记；
- 合并/归档/标记：按 ``audit_log.before`` 快照恢复；
- 取代：恢复旧条目的 ``valid_to`` / ``status`` / ``superseded_by``；
- 本次运行产生的 ``episode`` 与 ``DERIVED_FROM`` 边一并清理。

幂等键为 ``(job_type, window_start, window_end, scope)``，任务 id 由该键派生
稳定 UUID；重复调用（含 dry-run 之后）返回同一 ``job_run``，不会重复创建
情景或语义记忆。``dry_run=True`` 只读取与调用 LLM，不写任何表。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import uuid
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, not_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_current_settings
from app.errors import BadRequestError, ConflictError, NotFoundError
from app.models import (
    AuditLog,
    Episode,
    JobRun,
    Memory,
    MemoryEmbedding,
    MemoryEntity,
    MemoryLink,
)
from app.models import Session as SessionModel
from app.models.base import utcnow
from app.repositories.memory import MemoryRepository
from app.schemas.memory import ValidationError, normalize_content
from app.services.audit import ActorRef, actor_fields, write_audit
from app.services.memory_write import RuleConflictJudge, emit_memory_event

logger = logging.getLogger(__name__)

JOB_TYPE_CONSOLIDATE_SESSION = "consolidate_session"
JOB_TYPE_CONSOLIDATE_NIGHTLY = "consolidate_nightly"
CONSOLIDATION_JOB_TYPES = frozenset(
    {JOB_TYPE_CONSOLIDATE_SESSION, JOB_TYPE_CONSOLIDATE_NIGHTLY}
)

#: 与规格 §5.5 一致：同实体或嵌入余弦 ≥ 0.82 归入同一簇。
SIMILARITY_THRESHOLD = 0.82
#: 夜间任务只处理 ≥ 3 条的簇。
NIGHTLY_MIN_CLUSTER = 3
MAX_FACTS = 5
MAX_SUMMARY_CHARS = 2000
MAX_FACT_CHARS = 8192
MAX_CONTENT_CHARS = 8192
#: 回滚标记；只用于审计，不参与幂等判断。
ROLLED_BACK_KEY = "rolled_back_run_id"

CONSOLIDATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "facts"],
    "properties": {
        "summary": {"type": "string"},
        "facts": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM_PROMPT = """你是记忆巩固器。阅读一组工作记忆，写一段简洁的中文情景摘要，
并抽取 0 到 5 条值得长期保留的原子事实。只输出 JSON 对象，形如
{"summary": "...", "facts": ["..."]}，不要输出解释或 Markdown。
不要编造输入中没有的信息；没有可保留的事实时 facts 输出空数组。"""


@dataclass(frozen=True, slots=True)
class ConsolidationDiff:
    """一次巩固的计划或执行结果；dry-run 与正式运行共用同一结构。"""

    job_run_id: str | None
    job_type: str
    dry_run: bool = False
    replayed: bool = False
    created_episodes: int = 0
    created_memories: int = 0
    merged_memories: int = 0
    superseded_memories: int = 0
    archived_working: int = 0
    clusters: int = 0
    actions: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_run_id": self.job_run_id,
            "job_type": self.job_type,
            "dry_run": self.dry_run,
            "replayed": self.replayed,
            "created_episodes": self.created_episodes,
            "created_memories": self.created_memories,
            "merged_memories": self.merged_memories,
            "superseded_memories": self.superseded_memories,
            "archived_working": self.archived_working,
            "clusters": self.clusters,
            "actions": [dict(action) for action in self.actions],
        }


@dataclass(slots=True)
class _Plan:
    """尚未落库的巩固计划；dry-run 直接把它翻译成 :class:`ConsolidationDiff`。"""

    actions: list[dict[str, Any]] = field(default_factory=list)
    clusters: int = 0

    @property
    def creates(self) -> list[dict[str, Any]]:
        return [action for action in self.actions if action["action"] == "create"]

    @property
    def merges(self) -> list[dict[str, Any]]:
        return [action for action in self.actions if action["action"] == "merge"]

    @property
    def supersedes(self) -> list[dict[str, Any]]:
        return [
            action for action in self.actions if action["action"] == "supersede"
        ]

    @property
    def archives(self) -> list[dict[str, Any]]:
        return [action for action in self.actions if action["action"] == "archive"]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _snapshot(memory: Memory) -> dict[str, Any]:
    """回滚所需的完整记忆快照；比写入管线的快照多 metadata/summary。"""
    return {
        "content": memory.content,
        "summary": memory.summary,
        "type": memory.type,
        "scope": memory.scope,
        "scope_id": memory.scope_id,
        "importance": memory.importance,
        "confidence": memory.confidence,
        "stability": memory.stability,
        "access_count": memory.access_count,
        "version": memory.version,
        "status": memory.status,
        "valid_to": _iso(memory.valid_to),
        "superseded_by": memory.superseded_by,
        "metadata": _json_copy(memory.metadata_ or {}),
    }


def _audit_memory(
    db: Session,
    memory: Memory,
    *,
    run_id: str,
    before: dict[str, Any] | None,
    actor: ActorRef,
    detail: dict[str, Any] | None = None,
) -> None:
    after = _snapshot(memory)
    after["consolidation_run_id"] = run_id
    if detail:
        after["consolidation_action"] = detail.get("action")
        after["consolidation_reason"] = detail.get("reason")
    write_audit(
        db,
        action="memory.consolidated",
        target_type="memory",
        target_id=memory.id,
        workspace_id=memory.workspace_id,
        actor_type=actor.kind,
        actor_id=actor.id,
        before=before,
        after=after,
    )


def _publish(job_id: str, event: dict[str, Any]) -> None:
    """把任务进度发到 Redis pub/sub；失败不影响任务本身。"""
    try:
        import redis

        client = redis.Redis.from_url(get_current_settings().redis_url)
        try:
            client.publish(
                f"jobs:{job_id}:events",
                json.dumps(event, ensure_ascii=False, default=str),
            )
        finally:
            client.close()
    except Exception:  # Redis 不可用只是少了实时进度
        logger.warning("consolidation event publish failed", extra={"job_id": job_id})


def job_channel(job_id: str) -> str:
    return f"jobs:{job_id}:events"


def _idempotency_key(
    job_type: str,
    window_start: datetime,
    window_end: datetime,
    scope: str,
) -> str:
    return json.dumps(
        {
            "job_type": job_type,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "scope": scope,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def job_id_for(
    job_type: str,
    window_start: datetime,
    window_end: datetime,
    scope: str,
) -> str:
    """由幂等键派生稳定 UUID；并发/重复调用落到同一条 ``job_run``。"""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "agent-memory:consolidate:"
            + _idempotency_key(job_type, window_start, window_end, scope),
        )
    )


def _complete(llm: Any, messages: list[dict[str, str]]) -> str:
    """同步/异步 Provider 兼容；巩固任务运行在同步上下文。"""
    result = llm.complete(messages, CONSOLIDATION_SCHEMA)
    if not inspect.isawaitable(result):
        return str(result or "")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return str(asyncio.run(result) or "")
    with ThreadPoolExecutor(max_workers=1) as pool:
        return str(pool.submit(asyncio.run, result).result() or "")


def _decode(raw: str) -> tuple[str | None, list[str]]:
    text = (raw or "").strip()
    if not text:
        return None, []
    if text.startswith("```"):
        text = text.strip("`")
        _, _, text = text.partition("\n")
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None, []
    if not isinstance(data, dict):
        return None, []
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = None
    facts = data.get("facts")
    if not isinstance(facts, list):
        facts = []
    return (summary.strip()[:MAX_SUMMARY_CHARS] if summary else None), [
        str(item) for item in facts
    ]


def _template_summary(
    memories: Sequence[Memory], prefix: str = "会话", unit: str = "工作记忆"
) -> str:
    parts = []
    for memory in memories[:5]:
        text = " ".join(memory.content.split())
        parts.append(text[:120])
    body = "；".join(parts) or "无可用内容"
    return f"{prefix}共 {len(memories)} 条{unit}：{body}"[:MAX_SUMMARY_CHARS]


def _normalize_facts(raw_facts: Iterable[str]) -> list[str]:
    facts: list[str] = []
    seen: set[str] = set()
    for raw in raw_facts:
        if len(facts) >= MAX_FACTS:
            break
        try:
            content = normalize_content(str(raw)[:MAX_FACT_CHARS])
        except ValidationError:
            continue
        key = " ".join(content.split()).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        facts.append(content)
    return facts


def _generate_summary(
    memories: Sequence[Memory], *, prefix: str = "会话", unit: str = "工作记忆"
) -> tuple[str, list[str]]:
    """调用 LLM 生成摘要与事实；任何失败都退化为模板摘要。"""
    if not memories:
        return "", []
    prompt = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "\n".join(f"- {memory.content}" for memory in memories),
        },
    ]
    runtime_llm = _runtime_llm()
    if runtime_llm is None:
        return _template_summary(memories, prefix, unit), []
    try:
        summary, facts = _decode(_complete(runtime_llm, prompt))
    except Exception:
        logger.exception("consolidation llm failed; falling back to template")
        summary, facts = None, []
    return (summary or _template_summary(memories, prefix, unit)), _normalize_facts(
        facts
    )


def _runtime_llm() -> Any | None:
    from app.jobs.runtime import get_runtime

    return get_runtime().llm


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _cluster_memories(db: Session, memories: Sequence[Memory]) -> list[list[Memory]]:
    """同实体或嵌入余弦 ≥ 0.82 的并查集聚类；保持输入顺序。"""
    if not memories:
        return []
    order = {memory.id: index for index, memory in enumerate(memories)}
    parent = {memory.id: memory.id for memory in memories}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[max(root_left, root_right, key=lambda v: order[v])] = min(
                root_left, root_right, key=lambda v: order[v]
            )

    ids = list(order)
    entity_rows = db.execute(
        select(MemoryEntity.memory_id, MemoryEntity.entity_id).where(
            MemoryEntity.memory_id.in_(ids)
        )
    ).all()
    by_entity: dict[str, list[str]] = {}
    for memory_id, entity_id in entity_rows:
        by_entity.setdefault(str(entity_id), []).append(str(memory_id))
    for members in by_entity.values():
        for member in members[1:]:
            union(members[0], member)

    embedding_rows = db.execute(
        select(
            MemoryEmbedding.memory_id,
            MemoryEmbedding.model,
            MemoryEmbedding.vector,
        )
        .where(MemoryEmbedding.memory_id.in_(ids))
        .order_by(
            MemoryEmbedding.updated_at.desc(), MemoryEmbedding.model.asc()
        )
    ).all()
    vectors: dict[str, list[float]] = {}
    for memory_id, _model, vector in embedding_rows:
        vectors.setdefault(str(memory_id), list(vector))
    by_id = {memory.id: memory for memory in memories}
    vector_ids = sorted((mid for mid in vectors if mid in parent), key=order.get)
    for index, left in enumerate(vector_ids):
        for right in vector_ids[index + 1 :]:
            # 夜间全量巩固可能同时读多个 workspace；绝不跨租户聚类。
            if by_id[left].workspace_id != by_id[right].workspace_id:
                continue
            if _cosine(vectors[left], vectors[right]) >= SIMILARITY_THRESHOLD:
                union(left, right)

    grouped: dict[str, list[Memory]] = {}
    for memory in memories:
        grouped.setdefault(find(memory.id), []).append(memory)
    return sorted(
        grouped.values(), key=lambda group: order[group[0].id]
    )


def _entity_ids(db: Session, memory_ids: Sequence[str]) -> list[str]:
    if not memory_ids:
        return []
    return [
        str(value)
        for value in db.scalars(
            select(MemoryEntity.entity_id)
            .where(MemoryEntity.memory_id.in_(memory_ids))
            .distinct()
        )
    ]


def _scope_condition(scope_id: str | None) -> Any:
    if scope_id is None:
        return Memory.scope_id.is_(None)
    return Memory.scope_id == scope_id


def _find_candidates(
    db: Session,
    *,
    workspace_id: str,
    scope: str,
    scope_id: str | None,
    content: str,
    entity_ids: Sequence[str],
    exclude_ids: Sequence[str],
) -> list[tuple[Memory, float]]:
    """合并/取代候选：同实体或 pg_trgm 相似度 ≥ 0.82 的当前语义记忆。"""
    scored: dict[str, tuple[Memory, float]] = {}
    if entity_ids:
        statement = (
            select(Memory)
            .join(MemoryEntity, MemoryEntity.memory_id == Memory.id)
            .where(
                Memory.workspace_id == workspace_id,
                Memory.type == "semantic",
                Memory.status == "active",
                Memory.deleted_at.is_(None),
                Memory.scope == scope,
                _scope_condition(scope_id),
                MemoryEntity.entity_id.in_(list(entity_ids)),
                or_(Memory.valid_to.is_(None), Memory.valid_to > func.now()),
            )
            .order_by(Memory.importance.desc(), Memory.id.desc())
            .limit(10)
        )
        for memory in db.scalars(statement):
            if memory.id in exclude_ids:
                continue
            scored[memory.id] = (memory, 0.0)

    repo = MemoryRepository(db)
    for memory, score in repo.find_similar(
        workspace_id, [(scope, scope_id)], content, limit=5
    ):
        if memory.id in exclude_ids or memory.type != "semantic":
            continue
        if score < SIMILARITY_THRESHOLD:
            continue
        previous = scored.get(memory.id)
        if previous is None or score > previous[1]:
            scored[memory.id] = (memory, score)

    return sorted(
        scored.values(),
        key=lambda item: (item[1], item[0].importance),
        reverse=True,
    )


def _plan_facts(
    db: Session,
    *,
    workspace_id: str,
    scope: str,
    scope_id: str | None,
    facts: Sequence[str],
    entity_ids: Sequence[str],
    derived_from: Sequence[str],
    judge: RuleConflictJudge,
) -> list[dict[str, Any]]:
    """为每条事实决定 create/merge/supersede；不改库。"""
    actions: list[dict[str, Any]] = []
    planned_supersede: set[str] = set()
    for fact in facts:
        candidates = _find_candidates(
            db,
            workspace_id=workspace_id,
            scope=scope,
            scope_id=scope_id,
            content=fact,
            entity_ids=entity_ids,
            exclude_ids=sorted(planned_supersede),
        )
        if not candidates:
            actions.append(
                {
                    "action": "create",
                    "content": fact,
                    "workspace_id": workspace_id,
                    "scope": scope,
                    "scope_id": scope_id,
                    "entity_ids": list(entity_ids),
                    "derived_from": list(derived_from),
                }
            )
            continue

        contradicting = None
        for memory, _score in candidates:
            try:
                if judge.is_contradiction(fact, memory.content) is True:
                    contradicting = memory
                    break
            except Exception:  # judge 是不可信外部能力，失败退回合并
                logger.warning("consolidation conflict judge failed")
                break
        if contradicting is not None and contradicting.id not in planned_supersede:
            planned_supersede.add(contradicting.id)
            actions.append(
                {
                    "action": "supersede",
                    "content": fact,
                    "workspace_id": workspace_id,
                    "scope": scope,
                    "scope_id": scope_id,
                    "target_id": contradicting.id,
                    "entity_ids": list(entity_ids),
                    "derived_from": list(derived_from),
                    "reason": "规则判定与既有事实矛盾",
                }
            )
            continue

        target, score = candidates[0]
        actions.append(
            {
                "action": "merge",
                "content": fact,
                "workspace_id": workspace_id,
                "scope": scope,
                "scope_id": scope_id,
                "target_id": target.id,
                "entity_ids": list(entity_ids),
                "derived_from": list(derived_from),
                "similarity": score,
                "reason": "同实体或相似度达到聚类阈值",
            }
        )
    return actions


def _counts(plan: _Plan, *, episodes: int, archived: int) -> dict[str, int]:
    return {
        "created_episodes": episodes,
        "created_memories": episodes + len(plan.creates) + len(plan.supersedes),
        "merged_memories": len(plan.merges),
        "superseded_memories": len(plan.supersedes),
        "archived_working": archived,
        "clusters": plan.clusters,
    }


def _diff_from_plan(
    plan: _Plan,
    *,
    job_id: str | None,
    job_type: str,
    episodes: int,
    archived: int,
    dry_run: bool,
) -> ConsolidationDiff:
    counts = _counts(plan, episodes=episodes, archived=archived)
    return ConsolidationDiff(
        job_run_id=job_id,
        job_type=job_type,
        dry_run=dry_run,
        actions=tuple(plan.actions),
        **counts,
    )


def _start_job(
    db: Session,
    *,
    job_id: str,
    job_type: str,
    scope: str,
    params: dict[str, Any],
    actor: ActorRef,
    workspace_id: str,
) -> tuple[JobRun, bool]:
    """创建或复用任务行并置 running；返回 (job, replayed)。"""
    job = db.get(JobRun, job_id)
    if job is not None and job.status in {"completed", "rolled_back"}:
        return job, True
    if job is None:
        job = JobRun(
            id=job_id,
            type=job_type,
            scope=scope[:255],
            status="running",
            params=params,
            started_at=utcnow(),
        )
        db.add(job)
    else:
        job.status = "running"
        job.error = None
        job.result = None
        job.params = params
        job.started_at = utcnow()
        job.finished_at = None
    write_audit(
        db,
        action="consolidation.started",
        target_type="consolidation",
        target_id=job_id,
        workspace_id=workspace_id,
        actor_type=actor.kind,
        actor_id=actor.id,
        after={"job_type": job_type, "scope": scope, "params": params},
    )
    try:
        db.commit()
    except IntegrityError:
        # 并发触发同一幂等键：对方已创建任务，回查后决定重放还是继续。
        db.rollback()
        existing = db.get(JobRun, job_id)
        if existing is None:
            raise
        if existing.status in {"completed", "rolled_back"}:
            return existing, True
        job = existing
        job.status = "running"
        job.started_at = utcnow()
        job.finished_at = None
        job.error = None
        db.commit()
    _publish(job_id, {"job_id": job_id, "status": "running", "type": job_type})
    return job, False


def _finish_job(
    db: Session,
    job: JobRun,
    diff: ConsolidationDiff,
    *,
    actor: ActorRef,
    workspace_id: str,
) -> None:
    job.status = "completed"
    job.result = diff.to_dict()
    job.error = None
    job.finished_at = utcnow()
    write_audit(
        db,
        action="consolidation.completed",
        target_type="consolidation",
        target_id=job.id,
        workspace_id=workspace_id,
        actor_type=actor.kind,
        actor_id=actor.id,
        after=diff.to_dict(),
    )
    db.commit()
    _publish(
        job.id,
        {"job_id": job.id, "status": "completed", "result": diff.to_dict()},
    )


def mark_job_failed(db: Session, job_id: str, exc: Exception) -> None:
    """把失败写入 ``job_run.error``；任务本身仍可重试。"""
    job = db.get(JobRun, job_id)
    if job is None:
        return
    message = (str(exc) or exc.__class__.__name__)[:2000]
    job.status = "failed"
    job.error = message
    job.finished_at = utcnow()
    job.result = {**(job.result or {}), "error": message}
    write_audit(
        db,
        action="consolidation.failed",
        target_type="consolidation",
        target_id=job_id,
        workspace_id=(job.params or {}).get("workspace_id"),
        actor_type="system",
        after={"error": message},
    )
    db.commit()
    _publish(job_id, {"job_id": job_id, "status": "failed", "error": message})


# ---------------------------------------------------------------------------
# 写库动作
# ---------------------------------------------------------------------------


def _link(
    db: Session,
    *,
    from_id: str,
    to_id: str,
    relation: str,
    run_id: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    db.add(
        MemoryLink(
            from_id=from_id,
            to_id=to_id,
            relation=relation,
            weight=1.0,
            evidence={"run_id": run_id, **(evidence or {})},
        )
    )


def _link_entities(db: Session, memory_id: str, entity_ids: Sequence[str]) -> None:
    for entity_id in entity_ids:
        db.execute(
            pg_insert(MemoryEntity)
            .values(memory_id=memory_id, entity_id=entity_id, role="consolidated")
            .on_conflict_do_nothing(
                index_elements=[MemoryEntity.memory_id, MemoryEntity.entity_id]
            )
        )


def _create_memory(
    db: Session,
    *,
    run_id: str,
    workspace_id: str,
    scope: str,
    scope_id: str | None,
    memory_type: str,
    content: str,
    importance: float,
    metadata: dict[str, Any],
    idempotency_key: str,
    entity_ids: Sequence[str],
    derived_from: Sequence[str],
    actor: ActorRef,
) -> Memory | None:
    """创建记忆；同幂等键已存在时跳过（失败重试安全）。"""
    repo = MemoryRepository(db)
    existing = repo.get_by_idempotency_key(workspace_id, idempotency_key)
    if existing is not None:
        return None
    enriched = dict(metadata)
    enriched["consolidation_run_id"] = run_id
    enriched["write_action"] = "created"
    memory = Memory(
        workspace_id=workspace_id,
        scope=scope,
        scope_id=scope_id,
        type=memory_type,
        content=content,
        importance=importance,
        confidence=0.6,
        stability=1.0,
        status="active",
        source_type="consolidation",
        source_id=run_id,
        idempotency_key=idempotency_key,
        metadata_=enriched,
        consolidation_run_id=run_id,
    )
    repo.add(memory)
    _link_entities(db, memory.id, entity_ids)
    for source_id in derived_from:
        _link(
            db,
            from_id=memory.id,
            to_id=source_id,
            relation="DERIVED_FROM",
            run_id=run_id,
        )
    _audit_memory(
        db,
        memory,
        run_id=run_id,
        before=None,
        actor=actor,
        detail={"action": "create"},
    )
    emit_memory_event(db, memory, "created")
    return memory


def _merged_content(existing: str, fact: str) -> tuple[str, str]:
    """合并正文：近重复保留更完整的一条，同实体不同事实追加一行。"""
    if fact in existing:
        return existing, "duplicate"
    if " ".join(fact.split()).casefold() in existing.casefold():
        return existing, "duplicate"
    if len(existing) + 1 + len(fact) <= MAX_CONTENT_CHARS:
        return f"{existing}\n{fact}", "appended"
    if len(fact) > len(existing):
        return fact, "replaced"
    return existing, "kept"


def _merge_memory(
    db: Session,
    *,
    run_id: str,
    action: dict[str, Any],
    actor: ActorRef,
) -> bool:
    repo = MemoryRepository(db)
    target = repo.get(action["workspace_id"], action["target_id"])
    if target is None or target.status != "active":
        return False
    before = _snapshot(target)
    content, strategy = _merged_content(target.content, action["content"])
    metadata = dict(target.metadata_ or {})
    metadata["consolidation_run_id"] = run_id
    metadata["write_action"] = "merged"
    evidence = list(metadata.get("merge_evidence", []))
    evidence.append(
        {
            "content": action["content"],
            "run_id": run_id,
            "at": utcnow().isoformat(),
        }
    )
    metadata["merge_evidence"] = evidence[-20:]
    repo.update(
        target,
        content=content,
        access_count=target.access_count + 1,
        confidence=min(1.0, target.confidence + 0.05),
        last_reinforced_at=utcnow(),
        metadata=metadata,
        consolidation_run_id=run_id,
    )
    for source_id in action.get("derived_from", []):
        _link(
            db,
            from_id=target.id,
            to_id=source_id,
            relation="DERIVED_FROM",
            run_id=run_id,
        )
    _audit_memory(
        db,
        target,
        run_id=run_id,
        before=before,
        actor=actor,
        detail={"action": "merge", "reason": f"{action.get('reason')} ({strategy})"},
    )
    emit_memory_event(db, target, "merged")
    return True


def _supersede_memory(
    db: Session,
    *,
    run_id: str,
    action: dict[str, Any],
    actor: ActorRef,
) -> Memory | None:
    repo = MemoryRepository(db)
    old = repo.get(action["workspace_id"], action["target_id"])
    if old is None or old.status != "active":
        return None
    before = _snapshot(old)
    metadata = dict(old.metadata_ or {})
    metadata["consolidation_run_id"] = run_id
    repo.update(
        old,
        status="superseded",
        valid_to=utcnow(),
        metadata=metadata,
        consolidation_run_id=run_id,
    )
    new = _create_memory(
        db,
        run_id=run_id,
        workspace_id=action["workspace_id"],
        scope=action["scope"],
        scope_id=action["scope_id"],
        memory_type="semantic",
        content=action["content"],
        importance=0.7,
        metadata={"replaces": old.id},
        idempotency_key=_fact_key(run_id, "supersede", action["content"]),
        entity_ids=action.get("entity_ids", []),
        derived_from=action.get("derived_from", []),
        actor=actor,
    )
    if new is None:  # 幂等重放：保底仍恢复旧条目的取代关系
        return None
    repo.update(old, superseded_by=new.id)
    _audit_memory(
        db,
        old,
        run_id=run_id,
        before=before,
        actor=actor,
        detail={"action": "supersede", "reason": action.get("reason")},
    )
    emit_memory_event(db, old, "superseded")
    return new


def _archive_memory(
    db: Session, *, run_id: str, memory: Memory, actor: ActorRef
) -> bool:
    if memory.status != "active":
        return False
    before = _snapshot(memory)
    metadata = dict(memory.metadata_ or {})
    metadata["consolidation_run_id"] = run_id
    metadata["archived_reason"] = "consolidated"
    MemoryRepository(db).update(
        memory,
        status="archived",
        metadata=metadata,
        consolidation_run_id=run_id,
    )
    _audit_memory(
        db,
        memory,
        run_id=run_id,
        before=before,
        actor=actor,
        detail={"action": "archive"},
    )
    emit_memory_event(db, memory, "archived")
    return True


def _fact_key(run_id: str, kind: str, content: str) -> str:
    digest = hashlib.sha256(
        " ".join(content.split()).casefold().encode("utf-8")
    ).hexdigest()[:32]
    return f"consolidate:{run_id}:{kind}:{digest}"


def _create_episode(
    db: Session,
    *,
    run_id: str,
    session: SessionModel,
    summary: str,
    cluster_count: int,
    working_count: int,
    action: dict[str, Any],
    actor: ActorRef,
) -> tuple[Episode | None, Memory | None]:
    """创建 episode 与对应 episodic 记忆；同 run 重放时跳过。"""
    existing = db.scalar(
        select(Episode).where(
            Episode.session_id == session.id,
            Episode.metadata_["consolidation_run_id"].astext == run_id,
        )
    )
    if existing is not None:
        return existing, None

    started_at = _parse_datetime(action.get("started_at")) or session.started_at
    ended_at = _parse_datetime(action.get("ended_at")) or session.ended_at
    episode = Episode(
        session_id=session.id,
        title=summary[:255],
        summary=summary,
        started_at=started_at,
        ended_at=ended_at,
        outcome="consolidated",
        metadata_={
            "consolidation_run_id": run_id,
            "workspace_id": session.workspace_id,
            "cluster_count": cluster_count,
            "working_count": working_count,
        },
    )
    db.add(episode)
    db.flush()
    write_audit(
        db,
        action="episode.created",
        target_type="episode",
        target_id=episode.id,
        workspace_id=session.workspace_id,
        actor_type=actor.kind,
        actor_id=actor.id,
        after={
            "session_id": session.id,
            "consolidation_run_id": run_id,
            "summary": summary,
        },
    )

    memory = _create_memory(
        db,
        run_id=run_id,
        workspace_id=session.workspace_id,
        scope="session",
        scope_id=session.id,
        memory_type="episodic",
        content=summary,
        importance=0.5,
        metadata={"episode_id": episode.id, "session_id": session.id},
        idempotency_key=f"consolidate:{run_id}:episode",
        entity_ids=(),
        derived_from=action.get("derived_from", []),
        actor=actor,
    )
    return episode, memory


def _apply_plan(
    db: Session,
    plan: _Plan,
    *,
    run_id: str,
    actor: ActorRef,
    episode_context: dict[str, Any] | None = None,
) -> None:
    for action in plan.actions:
        kind = action["action"]
        if kind == "episode":
            if episode_context is None:
                continue
            _create_episode(
                db,
                run_id=run_id,
                session=episode_context["session"],
                summary=action["summary"],
                cluster_count=action.get("cluster_count", 0),
                working_count=action.get("working_count", 0),
                action=action,
                actor=actor,
            )
        elif kind == "create":
            _create_memory(
                db,
                run_id=run_id,
                workspace_id=action["workspace_id"],
                scope=action["scope"],
                scope_id=action["scope_id"],
                memory_type="semantic",
                content=action["content"],
                importance=0.6,
                metadata={},
                idempotency_key=_fact_key(run_id, "fact", action["content"]),
                entity_ids=action.get("entity_ids", []),
                derived_from=action.get("derived_from", []),
                actor=actor,
            )
        elif kind == "merge":
            _merge_memory(db, run_id=run_id, action=action, actor=actor)
        elif kind == "supersede":
            _supersede_memory(db, run_id=run_id, action=action, actor=actor)
        elif kind == "archive":
            memory = db.get(Memory, action["target_id"])
            if memory is not None:
                _archive_memory(db, run_id=run_id, memory=memory, actor=actor)
    db.flush()


# ---------------------------------------------------------------------------
# 会话巩固
# ---------------------------------------------------------------------------


def _working_memories(
    db: Session, session: SessionModel, *, active_only: bool
) -> list[Memory]:
    statement = (
        select(Memory)
        .where(
            Memory.workspace_id == session.workspace_id,
            Memory.type == "working",
            Memory.scope == "session",
            Memory.scope_id == session.id,
            Memory.deleted_at.is_(None),
        )
        .order_by(Memory.created_at.asc(), Memory.id.asc())
    )
    if active_only:
        statement = statement.where(Memory.status == "active")
    return list(db.scalars(statement))


def _session_window(
    session: SessionModel, memories: Sequence[Memory]
) -> tuple[datetime, datetime]:
    """稳定窗口：会话起点 → 工作记忆最后强化时间。"""
    window_start = session.started_at
    candidates = [
        memory.last_reinforced_at
        for memory in memories
        if memory.last_reinforced_at is not None
    ]
    if session.ended_at is not None:
        candidates.append(session.ended_at)
    window_end = max(candidates) if candidates else session.started_at
    if window_end < window_start:
        window_end = window_start
    return window_start, window_end


def _get_session(db: Session, session_id: str) -> SessionModel:
    session = db.get(SessionModel, session_id)
    if session is None:
        raise NotFoundError("会话不存在")
    return session


def session_job_id(db: Session, session_id: str) -> str | None:
    """按会话当前窗口推导稳定任务 id；任务失败时用于回写 error。"""
    session = db.get(SessionModel, session_id)
    if session is None:
        return None
    working = _working_memories(db, session, active_only=False)
    window_start, window_end = _session_window(session, working)
    return job_id_for(
        JOB_TYPE_CONSOLIDATE_SESSION,
        window_start,
        window_end,
        f"session:{session_id}",
    )


def consolidate_session(
    db: Session,
    session_id: str,
    dry_run: bool = False,
    *,
    workspace_id: str | None = None,
    actor: Any | None = None,
) -> ConsolidationDiff:
    """把一个会话的工作记忆巩固为情景摘要与语义事实。"""
    session = _get_session(db, session_id)
    if workspace_id is not None and session.workspace_id != workspace_id:
        raise NotFoundError("会话不存在")

    all_working = _working_memories(db, session, active_only=False)
    window_start, window_end = _session_window(session, all_working)
    scope = f"session:{session_id}"
    job_id = job_id_for(
        JOB_TYPE_CONSOLIDATE_SESSION, window_start, window_end, scope
    )
    actor_ref = actor_fields(actor) if actor is not None else ("system", None)
    audit_actor_ref = ActorRef(*actor_ref)

    if not all_working:
        return ConsolidationDiff(
            job_run_id=None, job_type=JOB_TYPE_CONSOLIDATE_SESSION, dry_run=dry_run
        )

    if not dry_run:
        job = db.get(JobRun, job_id)
        if job is not None and job.status in {"completed", "rolled_back"}:
            return ConsolidationDiff(
                job_run_id=job_id,
                job_type=JOB_TYPE_CONSOLIDATE_SESSION,
                dry_run=False,
                replayed=True,
            )

    working = [memory for memory in all_working if memory.status == "active"]
    if not working:
        return ConsolidationDiff(
            job_run_id=None, job_type=JOB_TYPE_CONSOLIDATE_SESSION, dry_run=dry_run
        )

    clusters = _cluster_memories(db, working)
    ordered = [memory for cluster in clusters for memory in cluster]
    summary, facts = _generate_summary(ordered)
    working_ids = [memory.id for memory in ordered]
    entity_ids = _entity_ids(db, working_ids)
    plan = _Plan(clusters=len(clusters))
    plan.actions.append(
        {
            "action": "episode",
            "summary": summary,
            "session_id": session.id,
            "started_at": _iso(session.started_at),
            "ended_at": _iso(session.ended_at or window_end),
            "cluster_count": len(clusters),
            "working_count": len(working),
            # 事实与情景记忆都从这些工作记忆派生。
            "derived_from": working_ids,
        }
    )
    plan.actions.extend(
        _plan_facts(
            db,
            workspace_id=session.workspace_id,
            scope="session",
            scope_id=session.id,
            facts=facts,
            entity_ids=entity_ids,
            derived_from=working_ids,
            judge=RuleConflictJudge(),
        )
    )
    plan.actions.extend(
        {"action": "archive", "target_id": memory.id} for memory in working
    )

    if dry_run:
        return _diff_from_plan(
            plan,
            job_id=None,
            job_type=JOB_TYPE_CONSOLIDATE_SESSION,
            episodes=1,
            archived=len(working),
            dry_run=True,
        )

    params = {
        "workspace_id": session.workspace_id,
        "session_id": session.id,
        "window_start": _iso(window_start),
        "window_end": _iso(window_end),
        "scope": scope,
        "dry_run": False,
    }
    job, replayed = _start_job(
        db,
        job_id=job_id,
        job_type=JOB_TYPE_CONSOLIDATE_SESSION,
        scope=scope,
        params=params,
        actor=audit_actor_ref,
        workspace_id=session.workspace_id,
    )
    if replayed:
        return ConsolidationDiff(
            job_run_id=job.id,
            job_type=JOB_TYPE_CONSOLIDATE_SESSION,
            replayed=True,
        )

    try:
        _apply_plan(
            db,
            plan,
            run_id=job.id,
            actor=audit_actor_ref,
            episode_context={"session": session},
        )
        session.summary = summary
        metadata = dict(session.metadata_ or {})
        metadata["last_consolidation_run_id"] = job.id
        session.metadata_ = metadata
        db.flush()
        diff = _diff_from_plan(
            plan,
            job_id=job.id,
            job_type=JOB_TYPE_CONSOLIDATE_SESSION,
            episodes=1,
            archived=len(working),
            dry_run=False,
        )
        _finish_job(
            db, job, diff, actor=audit_actor_ref, workspace_id=session.workspace_id
        )
        return diff
    except Exception as exc:
        db.rollback()
        try:
            mark_job_failed(db, job.id, exc)
        except Exception:  # 记录失败不能掩盖原始错误
            logger.exception("failed to mark consolidation job failed")
        raise


# ---------------------------------------------------------------------------
# 夜间巩固
# ---------------------------------------------------------------------------


def _nightly_sources(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    workspace_id: str | None,
) -> list[Memory]:
    statement = (
        select(Memory)
        .where(
            Memory.type == "episodic",
            Memory.status == "active",
            Memory.deleted_at.is_(None),
            Memory.created_at >= window_start,
            Memory.created_at <= window_end,
            not_(Memory.metadata_.has_key("nightly_consolidation_run_id")),
        )
        .order_by(Memory.created_at.asc(), Memory.id.asc())
    )
    if workspace_id is not None:
        statement = statement.where(Memory.workspace_id == workspace_id)
    return list(db.scalars(statement))


def _pick_scope(memories: Sequence[Memory]) -> tuple[str, str | None]:
    counts: dict[tuple[str, str | None], int] = {}
    for memory in memories:
        key = (memory.scope, memory.scope_id)
        counts[key] = counts.get(key, 0) + 1
    return max(counts.items(), key=lambda item: item[1])[0]


def consolidate_nightly(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    scope: str | None = None,
    dry_run: bool = False,
    *,
    actor: Any | None = None,
) -> ConsolidationDiff:
    """把窗口内未巩固的 episodic 按簇抽象为语义事实（簇 ≥ 3 才处理）。"""
    if window_end <= window_start:
        raise BadRequestError("window_end 必须晚于 window_start", code="invalid_window")
    job_scope = scope or "all"
    workspace_filter = scope if scope not in (None, "all") else None
    job_id = job_id_for(
        JOB_TYPE_CONSOLIDATE_NIGHTLY, window_start, window_end, job_scope
    )
    actor_ref = actor_fields(actor) if actor is not None else ("system", None)
    audit_actor_ref = ActorRef(*actor_ref)

    if not dry_run:
        job = db.get(JobRun, job_id)
        if job is not None and job.status in {"completed", "rolled_back"}:
            return ConsolidationDiff(
                job_run_id=job_id,
                job_type=JOB_TYPE_CONSOLIDATE_NIGHTLY,
                replayed=True,
            )

    sources = _nightly_sources(db, window_start, window_end, workspace_filter)
    clusters = [
        cluster
        for cluster in _cluster_memories(db, sources)
        if len(cluster) >= NIGHTLY_MIN_CLUSTER
    ]
    plan = _Plan(clusters=len(clusters))

    for cluster in clusters:
        summary, facts = _generate_summary(
            cluster, prefix="夜间巩固", unit="情景记忆"
        )
        working_ids = [memory.id for memory in cluster]
        entity_ids = _entity_ids(db, working_ids)
        cluster_scope, cluster_scope_id = _pick_scope(cluster)
        plan.actions.extend(
            _plan_facts(
                db,
                workspace_id=cluster[0].workspace_id,
                scope=cluster_scope,
                scope_id=cluster_scope_id,
                facts=facts,
                entity_ids=entity_ids,
                derived_from=working_ids,
                judge=RuleConflictJudge(),
            )
        )
        plan.actions.append(
            {
                "action": "nightly_mark",
                "target_ids": working_ids,
                "summary": summary,
            }
        )

    if dry_run:
        return _diff_from_plan(
            plan,
            job_id=None,
            job_type=JOB_TYPE_CONSOLIDATE_NIGHTLY,
            episodes=0,
            archived=0,
            dry_run=True,
        )

    workspace_ids = {memory.workspace_id for memory in sources}
    params = {
        "workspace_id": workspace_filter or "all",
        "window_start": _iso(window_start),
        "window_end": _iso(window_end),
        "scope": job_scope,
        "dry_run": False,
        "source_count": len(sources),
        "cluster_count": len(clusters),
    }
    job, replayed = _start_job(
        db,
        job_id=job_id,
        job_type=JOB_TYPE_CONSOLIDATE_NIGHTLY,
        scope=job_scope,
        params=params,
        actor=audit_actor_ref,
        workspace_id=workspace_filter or "all",
    )
    if replayed:
        return ConsolidationDiff(
            job_run_id=job.id, job_type=JOB_TYPE_CONSOLIDATE_NIGHTLY, replayed=True
        )

    try:
        _apply_plan(db, plan, run_id=job.id, actor=audit_actor_ref)
        for action in plan.actions:
            if action["action"] != "nightly_mark":
                continue
            for memory_id in action["target_ids"]:
                memory = db.get(Memory, memory_id)
                if memory is None or memory.status != "active":
                    continue
                before = _snapshot(memory)
                metadata = dict(memory.metadata_ or {})
                metadata["nightly_consolidation_run_id"] = job.id
                metadata["consolidation_run_id"] = job.id
                MemoryRepository(db).update(
                    memory,
                    metadata=metadata,
                    consolidation_run_id=job.id,
                )
                _audit_memory(
                    db,
                    memory,
                    run_id=job.id,
                    before=before,
                    actor=audit_actor_ref,
                    detail={"action": "nightly_source"},
                )
                emit_memory_event(db, memory, "updated")
        db.flush()
        diff = _diff_from_plan(
            plan,
            job_id=job.id,
            job_type=JOB_TYPE_CONSOLIDATE_NIGHTLY,
            episodes=0,
            archived=0,
            dry_run=False,
        )
        _finish_job(
            db,
            job,
            diff,
            actor=audit_actor_ref,
            workspace_id=workspace_filter or (next(iter(workspace_ids), "all")),
        )
        return diff
    except Exception as exc:
        db.rollback()
        try:
            mark_job_failed(db, job.id, exc)
        except Exception:  # 记录失败不能掩盖原始错误
            logger.exception("failed to mark consolidation job failed")
        raise


# ---------------------------------------------------------------------------
# 回滚
# ---------------------------------------------------------------------------


def _restore_snapshot(db: Session, memory: Memory, snapshot: dict[str, Any]) -> None:
    metadata = _json_copy(snapshot.get("metadata") or {})
    metadata.pop("consolidation_run_id", None)
    MemoryRepository(db).update(
        memory,
        content=snapshot["content"],
        summary=snapshot.get("summary"),
        type=snapshot.get("type", memory.type),
        scope=snapshot.get("scope", memory.scope),
        scope_id=snapshot.get("scope_id"),
        importance=float(snapshot.get("importance", memory.importance)),
        confidence=float(snapshot.get("confidence", memory.confidence)),
        stability=float(snapshot.get("stability", memory.stability)),
        access_count=int(snapshot.get("access_count", memory.access_count)),
        status=snapshot.get("status", "active"),
        valid_to=_parse_datetime(snapshot.get("valid_to")),
        superseded_by=snapshot.get("superseded_by"),
        deleted_at=None,
        metadata=metadata,
        consolidation_run_id=None,
    )
    emit_memory_event(db, memory, "updated")


def rollback_run(
    db: Session,
    job_run_id: str,
    *,
    workspace_id: str | None = None,
    actor: Any | None = None,
) -> int:
    """按 ``job_run.id`` 反向恢复一次巩固；返回被软删的新增记忆数。"""
    job = db.scalar(
        select(JobRun).where(JobRun.id == job_run_id).with_for_update()
    )
    if job is None or job.type not in CONSOLIDATION_JOB_TYPES:
        raise NotFoundError("任务不存在")
    params = dict(job.params or {})
    if workspace_id is not None and params.get("workspace_id") != workspace_id:
        raise NotFoundError("任务不存在")
    if job.status == "rolled_back":
        raise ConflictError("任务已回滚", code="already_rolled_back")
    if job.status != "completed":
        raise ConflictError("只有已完成的任务可以回滚", code="job_not_completed")

    actor_ref = ActorRef(*actor_fields(actor)) if actor is not None else ActorRef("system")
    audits = list(
        db.scalars(
            select(AuditLog)
            .where(
                AuditLog.action == "memory.consolidated",
                AuditLog.after["consolidation_run_id"].astext == job_run_id,
            )
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        )
    )

    seen: set[str] = set()
    created: list[str] = []
    restored: list[str] = []
    for entry in audits:
        if entry.target_id in seen:
            continue
        seen.add(entry.target_id)
        memory = db.get(Memory, entry.target_id)
        if memory is None:
            continue
        if entry.before is None:
            created.append(entry.target_id)
            metadata = dict(memory.metadata_ or {})
            metadata.pop("consolidation_run_id", None)
            metadata[ROLLED_BACK_KEY] = job_run_id
            MemoryRepository(db).update(
                memory,
                status="deleted",
                deleted_at=utcnow(),
                metadata=metadata,
                consolidation_run_id=None,
            )
            emit_memory_event(db, memory, "deleted", deleted=True)
            continue
        _restore_snapshot(db, memory, dict(entry.before))
        restored.append(entry.target_id)

    # 本次运行创建的情景行与派生边没有软删语义，直接物理清理。
    db.execute(
        delete(Episode).where(
            Episode.metadata_["consolidation_run_id"].astext == job_run_id
        )
    )
    db.execute(
        delete(MemoryLink).where(
            MemoryLink.evidence["run_id"].astext == job_run_id
        )
    )

    job.status = "rolled_back"
    result = dict(job.result or {})
    result["rolled_back"] = True
    result["rollback"] = {
        "created_deleted": len(created),
        "restored": len(restored),
        "memory_ids": created,
        "restored_ids": restored,
    }
    job.result = result
    job.finished_at = utcnow()
    write_audit(
        db,
        action="consolidation.rolled_back",
        target_type="consolidation",
        target_id=job.id,
        workspace_id=params.get("workspace_id"),
        actor_type=actor_ref.kind,
        actor_id=actor_ref.id,
        after=result["rollback"],
    )
    db.commit()
    _publish(
        job.id,
        {"job_id": job.id, "status": "rolled_back", "result": result},
    )
    return len(created)


# ---------------------------------------------------------------------------
# 触发辅助
# ---------------------------------------------------------------------------


def find_sessions_to_consolidate(
    db: Session,
    *,
    now: datetime | None = None,
    idle_minutes: int | None = None,
    limit: int = 50,
) -> list[str]:
    """会话已结束或工作记忆空闲超过阈值，且仍有 active 工作记忆。"""
    now = now or utcnow()
    if idle_minutes is None:
        idle_minutes = get_current_settings().consolidation_idle_minutes
    cutoff = now - timedelta(minutes=idle_minutes)
    active = (
        select(Memory.scope_id, func.max(Memory.last_reinforced_at).label("last_at"))
        .where(
            Memory.type == "working",
            Memory.scope == "session",
            Memory.scope_id.is_not(None),
            Memory.status == "active",
            Memory.deleted_at.is_(None),
        )
        .group_by(Memory.scope_id)
    ).subquery()
    rows = db.execute(
        select(active.c.scope_id)
        .join(
            SessionModel,
            SessionModel.id == active.c.scope_id,
        )
        .where(
            or_(
                SessionModel.ended_at.is_not(None),
                active.c.last_at <= cutoff,
            )
        )
        .order_by(active.c.last_at.asc())
        .limit(limit)
    ).all()
    return [str(row[0]) for row in rows]


__all__ = [
    "CONSOLIDATION_JOB_TYPES",
    "CONSOLIDATION_SCHEMA",
    "ConsolidationDiff",
    "JOB_TYPE_CONSOLIDATE_NIGHTLY",
    "JOB_TYPE_CONSOLIDATE_SESSION",
    "consolidate_nightly",
    "consolidate_session",
    "find_sessions_to_consolidate",
    "job_channel",
    "job_id_for",
    "mark_job_failed",
    "rollback_run",
    "session_job_id",
]
