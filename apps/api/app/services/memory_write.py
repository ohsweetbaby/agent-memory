"""记忆写入主链路。

一条记忆的创建/合并/取代都在**单个事务**内完成 ``memory + audit_log +
outbox_event`` 的写入；写路径不做嵌入、不等待 LLM，投影由 outbox worker
异步补齐。

冲突判定分两层：

1. 语义矛盾：由可注入的 :class:`ConflictJudge` 判定；命中时允许绕过
   pg_trgm 相似度门槛执行 supersede，但候选必须同 workspace、同 scope、同
   type。
2. 纯词面冲突：交给 ``memory_core.dedupe.decide_conflict``，保持任务 5 的
   确定性规则。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory_core.decay import DecayParams, apply_rehearsal
from memory_core.dedupe import (
    ConflictDecision,
    ConflictKind,
    ExistingCandidate,
    decide_conflict,
)
from memory_core.importance import ImportanceSignals, score_importance
from memory_core.types import MemoryType

from app.errors import NotFoundError
from app.models import Entity, Memory, MemoryEntity, Workspace
from app.models.base import utcnow
from app.repositories.memory import MemoryRepository
from app.repositories.outbox import OutboxRepository
from app.schemas.memory import MemoryWriteRequest
from app.services.audit import actor_fields, write_audit
from app.services.encoding import EncodedMemory, encode_memory

logger = logging.getLogger(__name__)


#: 语义矛盾判定接口；返回 ``None`` 表示“不确定”，退化为规则。
class ConflictJudge(Protocol):
    def is_contradiction(
        self, new_content: str, candidate_content: str
    ) -> bool | None: ...


_LOCATION_PATTERN = re.compile(
    r"(?:住在|居住在|搬到|迁到|搬去|移居到?)"
    r"([\u4e00-\u9fff]{2,8}?)"
    r"(?=工作|上班|生活|学习|读书|，|。|,|\.|$|[!?！？\s])"
)

_ZH_SUBJECT_PREDICATE = re.compile(
    r"^([\u4e00-\u9fffA-Za-z0-9_]{1,16}?)"
    r"(住在|位于|使用|需要|想要|名叫|来自|从事|喜欢|讨厌|叫|是|在|有|会|爱|用)"
    r"(.+)$"
)
_EN_SUBJECT_PREDICATE = re.compile(
    r"^([A-Za-z][\w .'-]{0,40}?)\s+"
    r"(is|are|was|were|likes?|loves?|lives?\s+in|works?\s+at|has|have)\s+"
    r"(.+)$",
    re.IGNORECASE,
)


def _location(content: str) -> str | None:
    match = _LOCATION_PATTERN.search(content)
    return match.group(1) if match else None


class RuleConflictJudge:
    """保守的规则 judge，仅在能确定互斥时返回 ``True``。

    当前只覆盖“同一居住地点被改成另一个地点”这一类高频事实变化；其余情况
    返回 ``None``，让写入管线退回 ``decide_conflict``，避免规则误杀。
    """

    def is_contradiction(
        self, new_content: str, candidate_content: str
    ) -> bool | None:
        old_location = _location(candidate_content)
        new_location = _location(new_content)
        if old_location and new_location and old_location != new_location:
            return True
        return None


_RULE_JUDGE = RuleConflictJudge()


def get_conflict_judge() -> ConflictJudge:
    """FastAPI 依赖：默认规则 judge；测试可覆盖该依赖注入 fake。"""
    return _RULE_JUDGE


@dataclass(frozen=True, slots=True)
class WriteResult:
    """写入结果；``id`` 在 merge 时是被合并到的目标记忆。"""

    id: str
    action: str
    superseded_ids: tuple[str, ...] = ()
    importance: float = 0.0


def extract_subject_predicate(content: str) -> tuple[str, str] | None:
    """从单句内容提取 ``(主语, 谓语+宾语)`` 的粗粒度结构。

    该结构只用于 ``decide_conflict`` 的同主谓过滤与审计，不追求语言学精度；
    提取失败返回 ``None``，此时调用方不做额外过滤。
    """
    text = content.strip()
    if not text:
        return None
    for pattern in (_ZH_SUBJECT_PREDICATE, _EN_SUBJECT_PREDICATE):
        match = pattern.match(text)
        if match:
            subject = match.group(1).strip()
            predicate = (match.group(2) + match.group(3)).strip()
            if subject and predicate:
                return subject, predicate
    return None


def _subject_predicate_from_memory(memory: Memory) -> tuple[str, str] | None:
    value = (memory.metadata_ or {}).get("subject_predicate")
    if isinstance(value, (list, tuple)) and len(value) == 2:
        subject, predicate = value
        if isinstance(subject, str) and isinstance(predicate, str):
            return subject, predicate
    return None


def _actor_fields(actor: Any) -> tuple[str, str | None]:
    """兼容旧调用点；主体解析统一在 :mod:`app.services.audit`。"""
    return actor_fields(actor)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _snapshot(memory: Memory) -> dict[str, Any]:
    return {
        "content": memory.content,
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
    }


def _audit(
    db: Session,
    memory: Memory,
    action: str,
    actor_type: str,
    actor_id: str | None,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    write_audit(
        db,
        action=action,
        target_type="memory",
        target_id=memory.id,
        workspace_id=memory.workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        before=before,
        after=after if after is not None else _snapshot(memory),
    )


def _emit(db: Session, memory: Memory, action: str, *, deleted: bool = False) -> None:
    """写 outbox 事件；payload 至少包含 memory_id 与 version。"""
    OutboxRepository(db).add(
        "memory.deleted" if deleted else "memory.upserted",
        memory.id,
        {
            "memory_id": memory.id,
            "version": memory.version,
            "workspace_id": memory.workspace_id,
            "action": action,
            "status": memory.status,
            "type": memory.type,
            "scope": memory.scope,
            "scope_id": memory.scope_id,
        },
    )


def emit_memory_event(
    db: Session, memory: Memory, action: str, *, deleted: bool = False
) -> None:
    """API 层复用的事件写入入口；调用方负责提交事务。"""
    _emit(db, memory, action, deleted=deleted)


def _persist_entities(
    db: Session, workspace_id: str, encoded: EncodedMemory, memory_id: str
) -> None:
    """按 (workspace_id, name) upsert 实体并建立关联。"""
    for entity in encoded.entities:
        statement = (
            pg_insert(Entity)
            .values(
                workspace_id=workspace_id,
                name=entity.name,
                type=entity.type,
            )
            .on_conflict_do_update(
                index_elements=[Entity.workspace_id, Entity.name],
                set_={"type": func.coalesce(Entity.type, entity.type)},
            )
            .returning(Entity.id)
        )
        entity_id = db.scalar(statement)
        if entity_id is None:
            continue
        db.execute(
            pg_insert(MemoryEntity)
            .values(memory_id=memory_id, entity_id=entity_id, role=entity.role)
            .on_conflict_do_nothing(
                index_elements=[MemoryEntity.memory_id, MemoryEntity.entity_id]
            )
        )


def _new_memory(encoded: EncodedMemory, action: str) -> Memory:
    metadata = dict(encoded.metadata)
    metadata["write_action"] = action
    subject_predicate = extract_subject_predicate(encoded.content)
    if subject_predicate is not None:
        metadata["subject_predicate"] = list(subject_predicate)

    return Memory(
        workspace_id=encoded.workspace_id,
        scope=encoded.scope.value,
        scope_id=encoded.scope_id,
        type=encoded.type.value,
        content=encoded.content,
        importance=encoded.importance,
        confidence=encoded.confidence,
        stability=encoded.stability,
        access_count=0,
        status="active",
        source_type=encoded.source_type,
        source_id=encoded.source_id,
        metadata_=metadata,
        valid_from=encoded.valid_from,
        valid_to=encoded.valid_to,
        last_reinforced_at=encoded.valid_from,
    )


def _find_candidates(
    repo: MemoryRepository, encoded: EncodedMemory
) -> list[tuple[Memory, float]]:
    scope_pairs = [(encoded.scope, encoded.scope_id)]
    rows = repo.find_similar(
        encoded.workspace_id,
        scope_pairs,
        encoded.content,
        limit=3,
    )
    # 语义判定需要看到词面相似度为 0 的候选（例：北京 → 上海），因此在
    # pg_trgm 结果之外补一小批同 scope/type 的当前有效记忆。
    recent = repo.find_active(
        encoded.workspace_id,
        scope_pairs,
        encoded.type.value,
        limit=5,
    )
    scored: dict[str, tuple[Memory, float]] = {
        memory.id: (memory, score)
        for memory, score in rows
        if memory.type == encoded.type.value
    }
    for memory in recent:
        scored.setdefault(memory.id, (memory, 0.0))
    return sorted(scored.values(), key=lambda item: item[1], reverse=True)


_SUBJECT_BEFORE_LOCATION = re.compile(
    r"^([\u4e00-\u9fffA-Za-z0-9_]{1,16}?)(?:住在|居住在|搬到|迁到|搬去|移居到?|位于)"
)
_SUBJECT_ADVERBS = re.compile(r"(?:现在|目前|最近|刚刚|已经|已|刚|现)$")


def _content_subject(content: str) -> str | None:
    """提取事实主语；覆盖写入管线未收录的移动类谓词。"""
    extracted = extract_subject_predicate(content)
    raw = extracted[0] if extracted is not None else None
    if raw is None:
        match = _SUBJECT_BEFORE_LOCATION.match(content.strip())
        raw = match.group(1).strip() if match else None
    return _SUBJECT_ADVERBS.sub("", raw) if raw else raw


def _subjects_compatible(new_content: str, old_content: str) -> bool:
    """双方都能提取主语时要求一致，避免“父母住北京”被“我搬到上海”取代。"""
    new_subject = _content_subject(new_content)
    old_subject = _content_subject(old_content)
    if new_subject is None or old_subject is None:
        return True
    return new_subject.casefold() == old_subject.casefold()


def _semantic_target(
    encoded: EncodedMemory,
    candidates: list[tuple[Memory, float]],
    judge: ConflictJudge,
) -> Memory | None:
    for memory, _ in candidates:
        if not _subjects_compatible(encoded.content, memory.content):
            continue
        try:
            verdict = judge.is_contradiction(encoded.content, memory.content)
        except Exception:  # judge 是不可信外部能力，失败必须退化到规则
            logger.warning(
                "conflict judge failed; falling back to rules",
                extra={"workspace_id": encoded.workspace_id},
            )
            return None
        if verdict is True:
            return memory
    return None


def _decide(
    encoded: EncodedMemory,
    candidates: list[tuple[Memory, float]],
    judge: ConflictJudge,
    *,
    temporal_change: bool = False,
) -> tuple[ConflictDecision, dict[str, Memory]]:
    by_id = {memory.id: memory for memory, _ in candidates}
    semantic = _semantic_target(encoded, candidates, judge)
    if semantic is not None:
        # 调用方明确声明这是偏好/事实的时间演进时，同样绕过词面阈值，但动作
        # 是 coexist（只关闭旧条有效期），而不是 supersede。
        kind = ConflictKind.COEXIST if temporal_change else ConflictKind.SUPERSEDE
        reason = (
            f"judge 判定与 {semantic.id} 语义相关且标记为时间演进，关闭旧有效期"
            if temporal_change
            else f"judge 判定与 {semantic.id} 语义矛盾，执行取代"
        )
        return ConflictDecision(kind, semantic.id, reason), by_id

    existing = [
        ExistingCandidate(
            id=memory.id,
            content=memory.content,
            similarity=score,
            subject_predicate=_subject_predicate_from_memory(memory),
            valid_to=memory.valid_to,
        )
        for memory, score in candidates
    ]
    decision = decide_conflict(
        encoded.content,
        existing,
        contradiction=False,
        temporal_change=temporal_change,
        new_subject_predicate=extract_subject_predicate(encoded.content),
    )
    return decision, by_id


def _replay(existing: Memory) -> WriteResult:
    action = str((existing.metadata_ or {}).get("write_action", "created"))
    return WriteResult(
        id=existing.id,
        action=action,
        superseded_ids=(),
        importance=existing.importance,
    )


def _idempotency_key(req: MemoryWriteRequest) -> str | None:
    key = req.idempotency_key.strip() if req.idempotency_key else None
    return key or None


def _finish(db: Session, commit: bool) -> None:
    """提交或仅 flush；``commit=False`` 供审核等外层事务复用。"""
    if commit:
        db.commit()
    else:
        db.flush()


def write_memory(
    db: Session,
    req: MemoryWriteRequest,
    actor: Any,
    *,
    judge: ConflictJudge | None = None,
    commit: bool = True,
) -> WriteResult:
    """写入一条记忆并提交事务。

    ``actor`` 可以是 :class:`AuthContext`、``AdminUser`` 或 ``ApiKey``；审计
    主体据此确定。并发写入同一 ``idempotency_key`` 时，唯一索引保证只有一条
    记忆落库，输掉竞争的一方回查后返回同一 ``id``。

    ``commit=False`` 只 flush，把提交权交给调用方，用于“记忆 + 待审状态 +
    审核审计”必须原子提交的场景。
    """
    encoded = encode_memory(req)
    key = _idempotency_key(req)
    repo = MemoryRepository(db)

    if key:
        existing = repo.get_by_idempotency_key(encoded.workspace_id, key)
        if existing is not None:
            return _replay(existing)

    if db.get(Workspace, encoded.workspace_id) is None:
        raise NotFoundError("工作区不存在")

    actor_type, actor_id = _actor_fields(actor)
    resolved_judge = judge or get_conflict_judge()

    try:
        return _write(
            db,
            repo,
            req,
            encoded,
            actor_type=actor_type,
            actor_id=actor_id,
            judge=resolved_judge,
            idempotency_key=key,
            commit=commit,
        )
    except IntegrityError:
        db.rollback()
        if key:
            existing = repo.get_by_idempotency_key(encoded.workspace_id, key)
            if existing is not None:
                return _replay(existing)
        raise


def _write(
    db: Session,
    repo: MemoryRepository,
    req: MemoryWriteRequest,
    encoded: EncodedMemory,
    *,
    actor_type: str,
    actor_id: str | None,
    judge: ConflictJudge,
    idempotency_key: str | None,
    commit: bool,
) -> WriteResult:
    candidates = _find_candidates(repo, encoded) if req.dedupe else []
    if candidates:
        decision, by_id = _decide(
            encoded,
            candidates,
            judge,
            temporal_change=req.temporal_change,
        )
    else:
        decision = ConflictDecision(
            ConflictKind.CREATE, None, "无候选或未启用去重"
        )
        by_id = {}

    if decision.kind is ConflictKind.MERGE and decision.target_id:
        target = by_id[decision.target_id]
        return _merge(
            db,
            repo,
            target,
            encoded,
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            commit=commit,
        )

    if decision.kind is ConflictKind.SUPERSEDE and decision.target_id:
        old = by_id[decision.target_id]
        return _supersede(
            db,
            repo,
            old,
            encoded,
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            commit=commit,
        )

    if decision.kind is ConflictKind.COEXIST and decision.target_id:
        old = by_id[decision.target_id]
        return _coexist(
            db,
            repo,
            old,
            encoded,
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            commit=commit,
        )

    memory = _new_memory(encoded, "created")
    memory.idempotency_key = idempotency_key
    repo.add(memory)
    _persist_entities(db, encoded.workspace_id, encoded, memory.id)
    _audit(db, memory, "memory.created", actor_type, actor_id)
    _emit(db, memory, "created")
    _finish(db, commit)
    return WriteResult(memory.id, "created", (), memory.importance)


def _merge(
    db: Session,
    repo: MemoryRepository,
    target: Memory,
    encoded: EncodedMemory,
    *,
    actor_type: str,
    actor_id: str | None,
    idempotency_key: str | None = None,
    commit: bool = True,
) -> WriteResult:
    before = _snapshot(target)
    metadata = dict(target.metadata_ or {})
    evidence = list(metadata.get("merge_evidence", []))
    evidence.append(
        {
            "content": encoded.content,
            "source_quote": encoded.metadata.get("source_quote"),
            "at": utcnow().isoformat(),
        }
    )
    metadata["merge_evidence"] = evidence[-20:]
    metadata["write_action"] = "merged"

    # 合并到已有行时把本次幂等键挂到保留行；若目标已有不同的键，则记录为
    # 别名，避免重试时再次合并。唯一索引仍只覆盖主键字段。
    if idempotency_key:
        if target.idempotency_key is None:
            target.idempotency_key = idempotency_key
        elif target.idempotency_key != idempotency_key:
            aliases = set(metadata.get("idempotency_aliases", []))
            aliases.add(idempotency_key)
            metadata["idempotency_aliases"] = sorted(aliases)

    # 新条目携带的 PII/语言/来源信息也要累加到保留条目，避免合并后丢失标记。
    for key in ("pii", "language", "source_quote"):
        if key in encoded.metadata:
            metadata[key] = encoded.metadata[key]
    for key in ("pii_types", "pii_fields", "entities"):
        if key in encoded.metadata:
            merged = set(metadata.get(key, []))
            merged.update(encoded.metadata[key])
            metadata[key] = sorted(merged)
    subject_predicate = extract_subject_predicate(encoded.content)
    if subject_predicate is not None and "subject_predicate" not in metadata:
        metadata["subject_predicate"] = list(subject_predicate)

    # 保留信息更全的一条：新内容更长时替换正文，否则保留旧正文。
    content = (
        encoded.content
        if len(encoded.content) > len(target.content)
        else target.content
    )
    repo.update(
        target,
        content=content,
        access_count=target.access_count + 1,
        confidence=min(1.0, target.confidence + 0.05),
        last_reinforced_at=utcnow(),
        metadata=metadata,
    )
    _persist_entities(db, encoded.workspace_id, encoded, target.id)
    _audit(
        db,
        target,
        "memory.merged",
        actor_type,
        actor_id,
        before=before,
        after=_snapshot(target),
    )
    _emit(db, target, "merged")
    _finish(db, commit)
    return WriteResult(target.id, "merged", (), target.importance)


def _supersede(
    db: Session,
    repo: MemoryRepository,
    old: Memory,
    encoded: EncodedMemory,
    *,
    actor_type: str,
    actor_id: str | None,
    idempotency_key: str | None,
    commit: bool = True,
) -> WriteResult:
    now = utcnow()
    old_before = _snapshot(old)

    memory = _new_memory(encoded, "superseded")
    memory.idempotency_key = idempotency_key
    repo.add(memory)
    _persist_entities(db, encoded.workspace_id, encoded, memory.id)
    _audit(db, memory, "memory.created", actor_type, actor_id)

    repo.update(
        old,
        valid_to=now,
        status="superseded",
        superseded_by=memory.id,
    )
    _audit(
        db,
        old,
        "memory.superseded",
        actor_type,
        actor_id,
        before=old_before,
        after=_snapshot(old),
    )

    _emit(db, memory, "superseded")
    _emit(db, old, "superseded")
    _finish(db, commit)
    return WriteResult(
        memory.id, "superseded", (old.id,), memory.importance
    )


def _coexist(
    db: Session,
    repo: MemoryRepository,
    old: Memory,
    encoded: EncodedMemory,
    *,
    actor_type: str,
    actor_id: str | None,
    idempotency_key: str | None,
    commit: bool = True,
) -> WriteResult:
    now = utcnow()
    old_before = _snapshot(old)

    memory = _new_memory(encoded, "coexist")
    memory.idempotency_key = idempotency_key
    repo.add(memory)
    _persist_entities(db, encoded.workspace_id, encoded, memory.id)
    _audit(db, memory, "memory.created", actor_type, actor_id)

    # 偏好演进：旧条目只关闭有效期，不标记 superseded。
    repo.update(old, valid_to=now)
    _audit(
        db,
        old,
        "memory.updated",
        actor_type,
        actor_id,
        before=old_before,
        after=_snapshot(old),
    )

    _emit(db, memory, "created")
    _emit(db, old, "updated")
    _finish(db, commit)
    return WriteResult(memory.id, "coexist", (), memory.importance)


def feedback_memory(
    db: Session,
    memory: Memory,
    *,
    helpful: bool,
    reason: str | None,
    actor: Any,
) -> Memory:
    """记录一次显式反馈；只有 helpful 才强化重要性与稳定性。"""
    actor_type, actor_id = _actor_fields(actor)
    before = _snapshot(memory)
    metadata = dict(memory.metadata_ or {})
    history = list(metadata.get("feedback", []))
    history.append(
        {
            "helpful": helpful,
            "reason": reason,
            "at": utcnow().isoformat(),
        }
    )
    metadata["feedback"] = history[-20:]

    updates: dict[str, Any] = {"metadata": metadata}
    if helpful:
        updates.update(
            importance=score_importance(
                memory.content,
                MemoryType(memory.type),
                ImportanceSignals(helpful_feedback=1),
                memory.importance,
            ),
            stability=apply_rehearsal(memory.stability, DecayParams()),
            access_count=memory.access_count + 1,
            last_reinforced_at=utcnow(),
        )

    MemoryRepository(db).update(memory, **updates)
    _audit(
        db,
        memory,
        "memory.feedback",
        actor_type,
        actor_id,
        before=before,
        after=_snapshot(memory),
    )
    _emit(db, memory, "feedback")
    db.commit()
    return memory


__all__ = [
    "ConflictJudge",
    "RuleConflictJudge",
    "WriteResult",
    "emit_memory_event",
    "extract_subject_predicate",
    "feedback_memory",
    "get_conflict_judge",
    "write_memory",
]
