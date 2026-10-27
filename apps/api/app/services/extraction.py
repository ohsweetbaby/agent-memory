"""对话抽取与待审分流。

数据流（规格 §3.4）：

1. ``ingest_conversation`` 以 ``sha256(session_id + 归一化消息 JSON)`` 为幂等键
   创建 ``job_run``，并投递 ``extract_conversation``；
2. worker 调用 LLM 抽取候选，严格按 schema 校验；解析失败进入死信并写审计，
   不向调用方抛 500；
3. 候选先与既有记忆和同批候选去重，再按
   ``confidence >= 0.75`` 且 ``importance >= 0.5`` 分流：
   自动入库继续走 :func:`app.services.memory_write.write_memory`，
   否则写入 ``review_item`` 等待人工审核。

M1 不归档对话原文：任务完成后从 ``job_run.params`` 中移除消息，只保留摘要；
失败任务保留原文以便重试与排障。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, Field, ValidationError as PydanticValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory_core.types import MemoryType, Scope

from app.auth.session import AuthContext
from app.config import get_current_settings
from app.errors import ApiError, NotFoundError
from app.jobs.tasks import TASK_EXTRACT_CONVERSATION, enqueue
from app.models import JobRun, Memory, ReviewItem, Workspace
from app.models.base import utcnow
from app.repositories.memory import MemoryRepository
from app.schemas.memory import (
    EntitySpec,
    MemoryWriteRequest,
    SourceSpec,
    ValidationError,
    normalize_content,
)
from app.services.audit import ActorRef, actor_fields, write_audit
from app.services.memory_write import WriteResult, write_memory

logger = logging.getLogger(__name__)

#: 与既有记忆的 pg_trgm 相似度达到该值即视为重复；同文本由精确匹配兜底。
DEDUPE_SIMILARITY = 0.95
#: ``job_run.type`` 使用规格 §11 的任务名，而不是 Celery task 全名。
JOB_TYPE_EXTRACT_CONVERSATION = "extract_conversation"
MAX_MESSAGES = 200
MAX_MESSAGE_CHARS = 20_000
MAX_CONVERSATION_CHARS = 32_000
MAX_EVIDENCE_CHARS = 2_000

SYSTEM_PROMPT = """你是记忆抽取器。从对话中抽取值得长期记住的原子事实、用户偏好、事件与技能。
只输出 JSON 数组，不要输出解释、Markdown 或代码围栏。每个元素包含：
- content: 一句自包含的陈述，最多 8192 字符；
- type: working|episodic|semantic|procedural|perceptual；
- importance: 0 到 1；
- confidence: 0 到 1；
- evidence: 支持该候选的原文片段，最多 2000 字符；
- entities: 实体数组，每项 {"name", "type", "role"}。
不要编造对话中没有的信息；没有值得记住的内容时输出 []。"""

#: 传给 LLM 的结构化输出 schema（严格模式要求所有字段必填）。
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "content",
            "type",
            "importance",
            "confidence",
            "evidence",
            "entities",
        ],
        "properties": {
            "content": {"type": "string"},
            "type": {
                "type": "string",
                "enum": [item.value for item in MemoryType],
            },
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {"type": ["string", "null"]},
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "type", "role"],
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": ["string", "null"]},
                        "role": {"type": ["string", "null"]},
                    },
                },
            },
        },
    },
}

_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


class ExtractionError(Exception):
    """LLM 输出或运行时配置不可用；调用方应转入死信而不是重试。"""


class ConversationMessage(BaseModel):
    """对话中的一条消息；M1 只用于一次抽取，不落消息表。"""

    role: str = Field(default="user", min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    ts: str | None = Field(default=None, max_length=64)

    @field_validator("role")
    @classmethod
    def _normalize_role(cls, value: str) -> str:
        role = value.strip().lower()
        if not role:
            raise ValueError("role 不能为空")
        return role

    @field_validator("content")
    @classmethod
    def _normalize_message(cls, value: str) -> str:
        text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            raise ValueError("消息内容不能为空")
        if "\x00" in text:
            raise ValueError("消息内容包含 NUL 字符")
        return text


class ConversationIngestRequest(BaseModel):
    """``conversation_ingest`` 的请求体；``workspace_id`` 由路由补全。"""

    workspace_id: str | None = Field(default=None, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    messages: list[ConversationMessage] = Field(
        min_length=1, max_length=MAX_MESSAGES
    )
    scope: Scope | None = None
    scope_id: str | None = Field(default=None, max_length=36)
    auto_accept: bool = True


class _ExtractedEntity(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: str | None = Field(default=None, max_length=64)
    role: str | None = Field(default=None, max_length=32)


class _CandidatePayload(BaseModel):
    """LLM 单个候选的 schema；越界即视为输出不可信。"""

    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    type: MemoryType = MemoryType.SEMANTIC
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: str | None = Field(default=None, max_length=MAX_EVIDENCE_CHARS)
    entities: list[_ExtractedEntity] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Candidate:
    """一条待分流的候选记忆。"""

    content: str
    type: str = MemoryType.SEMANTIC.value
    importance: float = 0.5
    confidence: float = 0.5
    evidence: str | None = None
    entities: tuple[EntitySpec, ...] = ()
    workspace_id: str | None = None
    scope: Scope = Scope.SESSION
    scope_id: str | None = None
    session_id: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class IngestResult:
    """入库结果；``candidate_count`` 在抽取完成前为 0。"""

    task_id: str
    candidate_count: int = 0


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    candidates: tuple[Candidate, ...]
    skipped: int = 0


def _normalized_messages(
    messages: Iterable[ConversationMessage | dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ConversationMessage):
            role, content, ts = message.role, message.content, message.ts
        else:
            role = str(message.get("role") or "user").strip().lower()
            content = str(message.get("content") or "").replace("\r\n", "\n")
            content = content.replace("\r", "\n").strip()
            ts = message.get("ts")
        if not content:
            continue
        result.append({"role": role, "content": content, "ts": ts})
    return result


def _resolve_scope(req: ConversationIngestRequest) -> tuple[Scope, str]:
    scope_id = req.scope_id.strip() if req.scope_id else None
    if req.scope is Scope.GLOBAL:
        raise ValidationError(
            "抽取不支持 global 作用域",
            details={"field": "scope"},
        )
    if req.scope is None:
        scope = Scope.USER if scope_id else Scope.SESSION
        return scope, scope_id or req.session_id
    if scope_id is None and req.scope is not Scope.SESSION:
        raise ValidationError(
            f"{req.scope.value} 作用域必须指定 scope_id",
            details={"field": "scope_id"},
        )
    return req.scope, scope_id or req.session_id


def conversation_digest(
    session_id: str, messages: list[dict[str, Any]]
) -> str:
    """幂等摘要：``sha256(session_id + 归一化消息 JSON)``。"""
    normalized = [
        {
            "role": str(item.get("role") or "user").strip().lower(),
            "content": str(item.get("content") or "").strip(),
            "ts": item.get("ts"),
        }
        for item in messages
    ]
    payload = json.dumps(
        {"session_id": session_id, "messages": normalized},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def task_id_for(workspace_id: str, digest: str) -> str:
    """由幂等摘要派生稳定 UUID，避免并发重复建任务。"""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"agent-memory:extract:{workspace_id}:{digest}",
        )
    )


def _invoke_llm(
    llm: Any, messages: list[dict[str, str]], schema: dict[str, Any]
) -> str:
    """兼容同步/异步 Provider；worker 为同步上下文。"""
    result = llm.complete(messages, schema)
    if not inspect.isawaitable(result):
        return str(result or "")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return str(asyncio.run(result) or "")
    with ThreadPoolExecutor(max_workers=1) as pool:
        return str(pool.submit(asyncio.run, result).result() or "")


def _conversation_text(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages:
        suffix = f" ({message['ts']})" if message.get("ts") else ""
        lines.append(f"{message['role']}: {message['content']}{suffix}")
    text = "\n".join(lines)
    # 只保留最近部分，避免超长对话撑爆上下文；抽取以最新事实为主。
    return text[-MAX_CONVERSATION_CHARS:]


def _decode_payload(raw: str) -> list[Any]:
    text = raw.strip()
    if not text:
        raise ExtractionError("LLM 输出为空")
    text = _FENCE.sub("", text).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExtractionError("LLM 输出不是合法 JSON") from exc

    if isinstance(data, dict):
        candidates = data.get("candidates")
        if isinstance(candidates, list):
            data = candidates
        elif "content" in data:
            data = [data]
        else:
            raise ExtractionError("LLM 输出缺少 candidates 数组")
    if not isinstance(data, list):
        raise ExtractionError("LLM 输出必须是 JSON 数组")
    return data


def _build_outcome(data: list[Any]) -> ExtractionOutcome:
    candidates: list[Candidate] = []
    skipped = 0
    for index, item in enumerate(data):
        try:
            payload = _CandidatePayload.model_validate(item)
        except PydanticValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            raise ExtractionError(
                f"候选 {index} 不符合 schema: {first.get('msg', 'invalid')}"
            ) from exc

        try:
            content = normalize_content(payload.content)
        except ValidationError as exc:
            # 单条候选非法（NUL/超长）不应让整批抽取失败，也不能落库。
            skipped += 1
            logger.warning(
                "extraction candidate rejected",
                extra={"index": index, "reason": exc.resolved_code},
            )
            continue

        evidence = (
            payload.evidence.strip()[:MAX_EVIDENCE_CHARS]
            if payload.evidence
            else None
        )
        entities = tuple(
            EntitySpec(name=entity.name, type=entity.type, role=entity.role)
            for entity in payload.entities
        )
        candidates.append(
            Candidate(
                content=content,
                type=payload.type.value,
                importance=payload.importance,
                confidence=payload.confidence,
                evidence=evidence,
                entities=entities,
            )
        )
    return ExtractionOutcome(tuple(candidates), skipped)


def _extract(messages: list[Any], llm: Any) -> ExtractionOutcome:
    normalized = _normalized_messages(messages)
    if not normalized:
        raise ExtractionError("对话消息为空")
    prompt = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _conversation_text(normalized)},
    ]
    raw = _invoke_llm(llm, prompt, EXTRACTION_SCHEMA)
    return _build_outcome(_decode_payload(raw))


def extract_candidates(messages: list[Any], llm: Any) -> list[Candidate]:
    """从对话中抽取候选；解析/schema 失败抛 :class:`ExtractionError`。"""
    return list(_extract(messages, llm).candidates)


def _dedupe_key(content: str) -> str:
    return " ".join(content.split()).casefold()


def _is_duplicate(
    db: Session,
    *,
    workspace_id: str,
    scope: Scope,
    scope_id: str | None,
    content: str,
) -> bool:
    """与既有记忆去重：同作用域精确相同，或 pg_trgm 相似度极高。"""
    statement = select(Memory.id).where(
        Memory.workspace_id == workspace_id,
        Memory.scope == scope.value,
        Memory.status == "active",
        Memory.deleted_at.is_(None),
        Memory.content == content,
    )
    if scope_id is None:
        statement = statement.where(Memory.scope_id.is_(None))
    else:
        statement = statement.where(Memory.scope_id == scope_id)
    if db.scalar(statement.limit(1)) is not None:
        return True

    rows = MemoryRepository(db).find_similar(
        workspace_id, [(scope, scope_id)], content, limit=3
    )
    return any(score >= DEDUPE_SIMILARITY for _, score in rows)


def _has_pending_review(db: Session, workspace_id: str, content: str) -> bool:
    """同 workspace 已有同内容待审项时不再重复入队（重试安全）。"""
    statement = select(ReviewItem.id).where(
        ReviewItem.workspace_id == workspace_id,
        ReviewItem.status == "pending",
        ReviewItem.candidate["content"].astext == content,
    )
    return db.scalar(statement.limit(1)) is not None


def accept_candidate(db: Session, candidate: Candidate, actor: Any) -> WriteResult:
    """把候选交给写入管线；绝不绕过 ``write_memory``。"""
    if not candidate.workspace_id:
        raise ValidationError(
            "候选缺少 workspace_id", details={"field": "workspace_id"}
        )
    try:
        memory_type = MemoryType(candidate.type)
        scope = Scope(candidate.scope)
    except ValueError as exc:
        raise ValidationError(
            "候选类型或作用域不合法", details={"field": "type"}
        ) from exc

    request = MemoryWriteRequest(
        workspace_id=candidate.workspace_id,
        content=candidate.content,
        type=memory_type,
        scope=scope,
        scope_id=candidate.scope_id,
        importance=candidate.importance,
        entities=list(candidate.entities),
        source=SourceSpec(
            type="extraction",
            id=candidate.session_id,
            quote=candidate.evidence,
        ),
        idempotency_key=candidate.idempotency_key,
    )
    return write_memory(db, request, actor)


def _candidate_payload(candidate: Candidate) -> dict[str, Any]:
    return {
        "content": candidate.content,
        "type": candidate.type,
        "importance": candidate.importance,
        "confidence": candidate.confidence,
        "entities": [
            {"name": entity.name, "type": entity.type, "role": entity.role}
            for entity in candidate.entities
        ],
        "scope": candidate.scope.value,
        "scope_id": candidate.scope_id,
        "session_id": candidate.session_id,
    }


def _store_for_review(db: Session, job: JobRun, candidate: Candidate) -> ReviewItem:
    item = ReviewItem(
        workspace_id=candidate.workspace_id or "",
        candidate=_candidate_payload(candidate),
        evidence={
            "quote": candidate.evidence,
            "session_id": candidate.session_id,
            "task_id": job.id,
        },
        confidence=candidate.confidence,
        status="pending",
    )
    db.add(item)
    db.flush()
    return item


def _run_extraction(
    db: Session, job: JobRun, params: dict[str, Any]
) -> dict[str, Any]:
    runtime_llm = _runtime_llm()
    if runtime_llm is None:
        raise ExtractionError("LLM Provider 未配置，无法抽取对话")

    messages = params.get("messages") or []
    outcome = _extract(messages, runtime_llm)

    settings = get_current_settings()
    workspace_id = str(params["workspace_id"])
    scope = Scope(str(params.get("scope") or Scope.SESSION.value))
    scope_id = params.get("scope_id")
    session_id = params.get("session_id")
    auto_accept = bool(params.get("auto_accept", True))
    actor = ActorRef(
        str(params.get("actor_type") or "system"),
        params.get("actor_id"),
    )

    batch_keys: set[str] = set()
    auto_accepted = 0
    reviewed = 0
    deduped = 0

    for index, candidate in enumerate(outcome.candidates):
        key = _dedupe_key(candidate.content)
        if key in batch_keys:
            deduped += 1
            continue
        batch_keys.add(key)

        if _is_duplicate(
            db,
            workspace_id=workspace_id,
            scope=scope,
            scope_id=scope_id,
            content=candidate.content,
        ) or _has_pending_review(db, workspace_id, candidate.content):
            deduped += 1
            continue

        enriched = replace(
            candidate,
            workspace_id=workspace_id,
            scope=scope,
            scope_id=scope_id,
            session_id=session_id,
            idempotency_key=(
                f"extract:{job.id}:{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}"
            ),
        )
        should_accept = (
            auto_accept
            and enriched.confidence >= settings.auto_accept_confidence
            and enriched.importance >= settings.auto_accept_importance
        )
        if should_accept:
            accept_candidate(db, enriched, actor)
            auto_accepted += 1
        else:
            _store_for_review(db, job, enriched)
            reviewed += 1
        logger.info(
            "extraction candidate triaged",
            extra={
                "task_id": job.id,
                "index": index,
                "auto_accepted": should_accept,
                "confidence": enriched.confidence,
                "importance": enriched.importance,
            },
        )

    summary = {
        "candidate_count": len(outcome.candidates),
        "auto_accepted": auto_accepted,
        "review": reviewed,
        "deduped": deduped,
        "skipped": outcome.skipped,
    }
    job.status = "completed"
    job.result = summary
    job.finished_at = utcnow()
    # M1 不保留对话原文；消息数保留用于审计与排障。
    job.params = {
        key: value for key, value in params.items() if key != "messages"
    } | {"message_count": len(messages)}
    write_audit(
        db,
        action="ingest.completed",
        target_type="ingest",
        target_id=job.id,
        workspace_id=workspace_id,
        actor_type=actor.kind,
        actor_id=actor.id,
        after=summary,
    )
    db.commit()
    return summary


def _runtime_llm() -> Any | None:
    from app.jobs.runtime import get_runtime

    return get_runtime().llm


def process_extraction_job(db: Session, job: JobRun) -> dict[str, Any]:
    """执行一个抽取任务；``ExtractionError`` 由 Celery 任务转死信。"""
    params = dict(job.params or {})
    job.status = "running"
    job.started_at = utcnow()
    job.error = None
    db.commit()
    return _run_extraction(db, job, params)


def mark_job_dead(db: Session, job_id: str, exc: Exception) -> None:
    """把无法重试的抽取失败写入死信并审计。"""
    job = db.get(JobRun, job_id)
    if job is None:
        return
    message = (str(exc) or exc.__class__.__name__)[:2000]
    params = dict(job.params or {})
    job.status = "dead"
    job.error = message
    job.finished_at = utcnow()
    job.result = {**(job.result or {}), "error": message}
    write_audit(
        db,
        action="ingest.failed",
        target_type="ingest",
        target_id=job.id,
        workspace_id=params.get("workspace_id"),
        actor_type=str(params.get("actor_type") or "system"),
        actor_id=params.get("actor_id"),
        after={"error": message},
    )
    db.commit()


def _enqueue_job(db: Session, job: JobRun, task_id: str) -> None:
    """投递抽取任务；broker 不可用时标记失败并返回 503。"""
    try:
        enqueue(TASK_EXTRACT_CONVERSATION, task_id)
    except Exception as exc:  # broker 不可用：调用方可用同一消息重试
        job.status = "failed"
        job.error = (str(exc) or exc.__class__.__name__)[:2000]
        job.finished_at = utcnow()
        db.commit()
        logger.exception("failed to enqueue extraction task")
        raise ApiError(
            "抽取任务入队失败，请稍后重试",
            status_code=503,
            code="queue_unavailable",
        ) from exc


def ingest_conversation(
    db: Session, req: ConversationIngestRequest, actor: Any
) -> IngestResult:
    """登记一次对话抽取；相同 session + 消息重复调用返回同一 task_id。"""
    if not req.workspace_id:
        raise ValidationError(
            "必须指定 workspace_id", details={"field": "workspace_id"}
        )
    if (
        isinstance(actor, AuthContext)
        and actor.workspace_id is not None
        and actor.workspace_id != req.workspace_id
    ):
        # 跨 workspace 一律 404，避免枚举。
        raise NotFoundError("工作区不存在")
    if db.get(Workspace, req.workspace_id) is None:
        raise NotFoundError("工作区不存在")

    scope, scope_id = _resolve_scope(req)
    messages = _normalized_messages(req.messages)
    digest = conversation_digest(req.session_id, messages)
    task_id = task_id_for(req.workspace_id, digest)
    actor_type, actor_id = actor_fields(actor)

    existing = db.get(JobRun, task_id)
    if existing is not None:
        if existing.status in {"failed", "dead"}:
            existing.status = "pending"
            existing.error = None
            existing.result = None
            existing.started_at = None
            existing.finished_at = None
            db.commit()
            _enqueue_job(db, existing, task_id)
        return IngestResult(task_id=task_id, candidate_count=0)

    job = JobRun(
        id=task_id,
        type=JOB_TYPE_EXTRACT_CONVERSATION,
        scope=f"session:{req.session_id}"[:255],
        status="pending",
        params={
            "workspace_id": req.workspace_id,
            "session_id": req.session_id,
            "messages": messages,
            "message_count": len(messages),
            "scope": scope.value,
            "scope_id": scope_id,
            "auto_accept": req.auto_accept,
            "digest": digest,
            "actor_type": actor_type,
            "actor_id": actor_id,
        },
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        # 并发提交相同消息：对方已建任务，回查后复用。
        db.rollback()
        return IngestResult(task_id=task_id, candidate_count=0)

    write_audit(
        db,
        action="ingest.created",
        target_type="ingest",
        target_id=task_id,
        workspace_id=req.workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        after={
            "session_id": req.session_id,
            "message_count": len(messages),
            "scope": scope.value,
            "scope_id": scope_id,
            "auto_accept": req.auto_accept,
        },
    )
    db.commit()

    _enqueue_job(db, job, task_id)
    return IngestResult(task_id=task_id, candidate_count=0)


__all__ = [
    "Candidate",
    "ConversationIngestRequest",
    "ConversationMessage",
    "EXTRACTION_SCHEMA",
    "ExtractionError",
    "IngestResult",
    "JOB_TYPE_EXTRACT_CONVERSATION",
    "accept_candidate",
    "conversation_digest",
    "extract_candidates",
    "ingest_conversation",
    "mark_job_dead",
    "process_extraction_job",
    "task_id_for",
]
