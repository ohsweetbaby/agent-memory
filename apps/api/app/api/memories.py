"""记忆 REST 资源：写入、浏览、更新、置顶、合并、反馈与删除。

写入路径统一走 :func:`app.services.memory_write.write_memory`，保证
``memory + audit_log + outbox_event`` 同事务；本模块只负责认证、作用域校验
与响应组装。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, aliased

from memory_core.types import Scope

from app.api.deps import (
    DEFAULT_PAGE_SIZE,
    audit_actor,
    ensure_workspace,
    paginate,
    resolve_workspace_id,
    workspace_filter,
)
from app.auth.session import AuthContext, require_auth, require_scope
from app.db import get_db
from app.errors import BadRequestError, ForbiddenError, NotFoundError
from app.models import AuditLog, Memory, MemoryEmbedding, MemoryEntity, MemoryLink
from app.models.base import utcnow
from app.repositories.memory import MemoryRepository
from app.repositories.outbox import OutboxRepository
from app.schemas.memory import MemoryWriteRequest, normalize_content
from app.services.audit import write_audit
from app.services.encoding import detect_pii
from app.services.privacy import redact_related_records
from app.services.lifecycle import (
    cascade_hard_delete,
    memory_asset_paths,
)
from app.services.memory_write import (
    emit_memory_event,
    feedback_memory,
    get_conflict_judge,
    write_memory,
)

router = APIRouter(prefix="/api/v1/memories", tags=["memories"])

MAX_HISTORY_ITEMS = 20


class MemoryPatch(BaseModel):
    content: str | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    summary: str | None = None
    tags: list[str] | None = None
    valid_to: datetime | None = None


class FeedbackRequest(BaseModel):
    helpful: bool
    reason: str | None = Field(default=None, max_length=2000)


class MergeRequest(BaseModel):
    source_ids: list[str] = Field(min_length=1)
    target_id: str


class ForgetRequest(BaseModel):
    """按查询条件批量遗忘；管理员会话可跨 workspace。"""

    query: str = Field(min_length=1, max_length=500)
    scope: str | None = None
    scope_id: str | None = Field(default=None, max_length=36)
    memory_type: str | None = Field(default=None, alias="type")
    workspace_id: str | None = Field(default=None, max_length=36)
    hard: bool = False
    confirm: bool = False


def require_write(context: AuthContext = Depends(require_auth)) -> AuthContext:
    """写操作要求 write scope；错误码与读 Key 的 403 区分开。"""
    if not context.has_scope("write"):
        raise ForbiddenError("需要 write 权限", code="insufficient_scope")
    return context


def _load_memory(session: Session, context: AuthContext, memory_id: str) -> Memory:
    """按认证主体读取记忆；软删与跨 workspace 统一 404。"""
    if context.workspace_id is not None:
        memory = MemoryRepository(session).get(context.workspace_id, memory_id)
    else:
        # 单管理员会话可跨工作区管理（T8 裁决）；API Key 始终走上面的分支。
        memory = session.get(Memory, memory_id)
    if (
        memory is None
        or memory.status in {"deleted", "purged"}
        or memory.deleted_at is not None
    ):
        raise NotFoundError("记忆不存在")
    return memory


def _memory_payload(memory: Memory) -> dict[str, Any]:
    return {
        "id": memory.id,
        "workspace_id": memory.workspace_id,
        "scope": memory.scope,
        "scope_id": memory.scope_id,
        "type": memory.type,
        "content": memory.content,
        "summary": memory.summary,
        "importance": memory.importance,
        "confidence": memory.confidence,
        "stability": memory.stability,
        "access_count": memory.access_count,
        "version": memory.version,
        "status": memory.status,
        "superseded_by": memory.superseded_by,
        "valid_from": memory.valid_from,
        "valid_to": memory.valid_to,
        "source_type": memory.source_type,
        "source_id": memory.source_id,
        "metadata": memory.metadata_ or {},
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "deleted_at": memory.deleted_at,
    }


# 审计快照可能包含正文、来源原话与 PII；管理台只展示“改了什么”，
# 不回传 before/after 原文。
_AUDIT_SENSITIVE_KEYS = {"content", "summary", "metadata", "source_id"}


def _audit_payload(entry: AuditLog) -> dict[str, Any]:
    before = entry.before if isinstance(entry.before, dict) else {}
    after = entry.after if isinstance(entry.after, dict) else {}
    changed: list[str] = []
    if before.get("content") != after.get("content") and (
        "content" in before or "content" in after
    ):
        changed.append("content")
    for key in sorted(set(before) | set(after)):
        if key in _AUDIT_SENSITIVE_KEYS or key == "content":
            continue
        if before.get(key) != after.get(key):
            changed.append(key)
    return {
        "id": entry.id,
        "action": entry.action,
        "actor_type": entry.actor_type,
        "changed_fields": changed,
        "created_at": entry.created_at,
    }


def _contains_pattern(value: str) -> str:
    """转义 LIKE 通配符，避免 ``query=%`` 一次匹配整个 workspace。"""
    escaped = (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _audit_snapshot(memory: Memory) -> dict[str, Any]:
    return {
        "content": memory.content,
        "summary": memory.summary,
        "importance": memory.importance,
        "status": memory.status,
        "version": memory.version,
        "valid_to": memory.valid_to.isoformat() if memory.valid_to else None,
        "metadata": memory.metadata_ or {},
    }


def _mark_hard_deleted(
    session: Session,
    memory: Memory,
    *,
    actor_type: str,
    actor_id: str | None,
    query: str | None = None,
) -> tuple[str, list[str]]:
    """把硬删除落成 ``purged`` 墓碑并写 outbox，返回资产路径。

    规格 §10.4 要求 Postgres 先标记 ``purged``、同事务写 outbox；worker 再
    清理 Qdrant/Neo4j/资产。保留墓碑使对账与审计仍可追溯，重复硬删除因
    API 层 404 而天然幂等。
    """
    memory_id = memory.id
    workspace_id = memory.workspace_id
    asset_paths = memory_asset_paths(memory)
    purged_at = utcnow().isoformat()
    before = {
        "content": "[purged]",
        "summary": None,
        "importance": memory.importance,
        "status": memory.status,
        "version": memory.version,
    }
    # 硬删除必须在事实源里真正抹掉正文与派生行；只保留对账所需的最小墓碑。
    MemoryRepository(session).update(
        memory,
        status="purged",
        content="",
        summary=None,
        deleted_at=utcnow(),
        metadata={"purged_at": purged_at, "asset_paths": asset_paths},
    )
    session.execute(
        delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == memory_id)
    )
    session.execute(delete(MemoryEntity).where(MemoryEntity.memory_id == memory_id))
    session.execute(
        delete(MemoryLink).where(
            or_(MemoryLink.from_id == memory_id, MemoryLink.to_id == memory_id)
        )
    )
    # 历史审计快照可能含正文；删除目标记忆的旧审计，仅保留本次最小删除记录。
    session.execute(
        delete(AuditLog).where(
            AuditLog.target_type == "memory", AuditLog.target_id == memory_id
        )
    )
    redact_related_records(session, memory_id, workspace_id)
    write_audit(
        session,
        action="memory.deleted",
        target_type="memory",
        target_id=memory_id,
        workspace_id=workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        before=before,
        after={
            "content": "[purged]",
            "status": "purged",
            "version": memory.version,
            "hard": True,
            "purged": True,
            **({"query": query} if query else {}),
        },
    )
    OutboxRepository(session).add(
        "memory.deleted",
        memory_id,
        {
            "memory_id": memory_id,
            "version": memory.version,
            "workspace_id": workspace_id,
            "action": "purged",
            "hard": True,
            "asset_paths": asset_paths,
        },
    )
    return memory_id, asset_paths


def _history_payload(
    session: Session, memory: Memory
) -> list[dict[str, Any]]:
    """沿 ``superseded_by`` 反向收集被取代链，最旧的一条在前。"""
    history: list[dict[str, Any]] = []
    seen = {memory.id}
    current = memory
    while len(history) < MAX_HISTORY_ITEMS:
        previous = session.scalars(
            select(Memory)
            .where(
                Memory.workspace_id == memory.workspace_id,
                Memory.superseded_by == current.id,
                Memory.id != current.id,
                Memory.status.not_in(("deleted", "purged")),
                Memory.deleted_at.is_(None),
            )
            .order_by(Memory.created_at.asc(), Memory.id.asc())
        ).first()
        if previous is None or previous.id in seen:
            break
        seen.add(previous.id)
        history.insert(0, _memory_payload(previous))
        current = previous
    return history


def _link_statement(workspace_id: str, *conditions: Any) -> Any:
    """构造限定在同一 workspace 两端的边查询。

    ``memory_link`` 没有 workspace 列，通过 join 两端 ``memory`` 校验归属，
    避免跨 workspace 的脏边被详情返回或合并迁移。
    """
    from_memory = aliased(Memory)
    to_memory = aliased(Memory)
    return (
        select(MemoryLink)
        .join(from_memory, MemoryLink.from_id == from_memory.id)
        .join(to_memory, MemoryLink.to_id == to_memory.id)
        .where(
            from_memory.workspace_id == workspace_id,
            to_memory.workspace_id == workspace_id,
            *conditions,
        )
    )


def _links_payload(session: Session, memory: Memory) -> list[dict[str, Any]]:
    """返回与当前记忆直接相连的同 workspace 边，含出边与入边。"""
    rows = session.scalars(
        _link_statement(
            memory.workspace_id,
            or_(
                MemoryLink.from_id == memory.id,
                MemoryLink.to_id == memory.id,
            ),
        ).order_by(MemoryLink.created_at.asc(), MemoryLink.id.asc())
    ).all()
    return [
        {
            "id": link.id,
            "from_id": link.from_id,
            "to_id": link.to_id,
            "relation": link.relation,
            "weight": link.weight,
            "evidence": link.evidence,
        }
        for link in rows
    ]


def _migrate_links(
    session: Session, source_id: str, target_id: str, workspace_id: str
) -> None:
    """把 source 的边迁到 target。

    只处理同一 workspace 内、与 source 关联的边；与 target 已有等价边重复时
    删除 source 边，迁移后产生的自环也一并删除。绝不全局删除
    ``memory_link``，也不触碰跨 workspace 的边。
    """
    if source_id == target_id:
        return

    source_links = list(
        session.scalars(
            _link_statement(
                workspace_id,
                or_(
                    MemoryLink.from_id == source_id,
                    MemoryLink.to_id == source_id,
                ),
            )
        )
    )
    existing = {
        (link.from_id, link.to_id, link.relation)
        for link in session.scalars(
            _link_statement(
                workspace_id,
                or_(
                    MemoryLink.from_id == target_id,
                    MemoryLink.to_id == target_id,
                ),
            )
        )
    }

    for link in source_links:
        new_from = target_id if link.from_id == source_id else link.from_id
        new_to = target_id if link.to_id == source_id else link.to_id
        key = (new_from, new_to, link.relation)
        if new_from == new_to or key in existing:
            session.delete(link)
            continue
        existing.add(key)
        link.from_id = new_from
        link.to_id = new_to
    session.flush()


@router.get("")
def list_memories(
    workspace_id: str | None = Query(default=None, max_length=36),
    memory_type: str | None = Query(default=None, alias="type"),
    scope: str | None = Query(default=None),
    status: str | None = Query(default=None),
    min_importance: float | None = Query(default=None, ge=0.0, le=1.0),
    q: str | None = Query(default=None, max_length=500),
    include_history: bool = Query(default=False),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=DEFAULT_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    resolved = workspace_filter(context, workspace_id)
    statement = select(Memory)
    if resolved is not None:
        ensure_workspace(session, context, resolved)
        statement = statement.where(Memory.workspace_id == resolved)
    statement = statement.where(
        Memory.status.not_in(("deleted", "purged")),
        Memory.deleted_at.is_(None),
    )
    if memory_type is not None:
        statement = statement.where(Memory.type == memory_type)
    if scope is not None:
        statement = statement.where(Memory.scope == scope)
    if status is not None:
        statement = statement.where(Memory.status == status)
    else:
        if not include_archived:
            statement = statement.where(Memory.status != "archived")
        if not include_history:
            statement = statement.where(Memory.status != "superseded")
    if min_importance is not None:
        statement = statement.where(Memory.importance >= min_importance)
    if q and q.strip():
        statement = statement.where(Memory.content.ilike(f"%{q.strip()}%"))
    # ``status=active`` 不代表“当前有效”；默认排除 valid_to 已过期的记忆。
    # 显式请求 superseded 时不再按有效期裁剪，否则历史条目永远查不到。
    if not include_history and status != "superseded":
        now = utcnow()
        statement = statement.where(
            or_(Memory.valid_to.is_(None), Memory.valid_to > now)
        )

    items, next_cursor = paginate(
        session, statement, Memory, limit=limit, cursor=cursor
    )
    return {
        "items": [_memory_payload(item) for item in items],
        "next_cursor": next_cursor,
    }


@router.post("", status_code=201)
def create_memory(
    payload: MemoryWriteRequest,
    context: AuthContext = Depends(require_write),
    session: Session = Depends(get_db),
    judge=Depends(get_conflict_judge),
) -> dict[str, Any]:
    resolved = resolve_workspace_id(context, payload.workspace_id)
    ensure_workspace(session, context, resolved)
    if payload.scope == Scope.GLOBAL and not context.has_scope("admin"):
        raise ForbiddenError(
            "global 作用域仅 admin key 可写", code="insufficient_scope"
        )

    request = payload.model_copy(update={"workspace_id": resolved})
    result = write_memory(session, request, context, judge=judge)
    memory = session.get(Memory, result.id)
    if memory is None:  # pragma: no cover - 同事务刚写入，防御性分支
        raise NotFoundError("记忆不存在")
    body = _memory_payload(memory)
    body["action"] = result.action
    body["superseded_ids"] = list(result.superseded_ids)
    return body


@router.post("/merge")
def merge_memories(
    payload: MergeRequest,
    context: AuthContext = Depends(require_write),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    target = _load_memory(session, context, payload.target_id)
    if target.id in payload.source_ids:
        raise BadRequestError("target 不能同时作为 source", code="invalid_merge")
    if len(set(payload.source_ids)) != len(payload.source_ids):
        raise BadRequestError("source_ids 不能重复", code="invalid_merge")

    sources = [
        _load_memory(session, context, source_id)
        for source_id in payload.source_ids
    ]
    for source in sources:
        if source.workspace_id != target.workspace_id:
            raise NotFoundError("记忆不存在")

    actor_type, actor_id = audit_actor(context)
    repo = MemoryRepository(session)
    target_before = _audit_snapshot(target)
    target_metadata = dict(target.metadata_ or {})
    evidence = list(target_metadata.get("merge_evidence", []))
    access_count = target.access_count
    merged_ids: list[str] = []

    for source in sources:
        source_metadata = dict(source.metadata_ or {})
        source_metadata["merged_into"] = target.id
        source_before = _audit_snapshot(source)
        repo.update(source, status="archived", metadata=source_metadata)
        write_audit(
            session,
            action="memory.merged",
            target_type="memory",
            target_id=source.id,
            workspace_id=source.workspace_id,
            actor_type=actor_type,
            actor_id=actor_id,
            before=source_before,
            after={"merged_into": target.id},
        )
        emit_memory_event(session, source, "merged")

        # 只迁移 source 的边，避免全局删除；重复边与自环就地清理。
        _migrate_links(session, source.id, target.id, source.workspace_id)

        access_count += source.access_count
        evidence.append(
            {
                "source_id": source.id,
                "content": source.content,
                "at": utcnow().isoformat(),
            }
        )
        merged_ids.append(source.id)

    target_metadata["merge_evidence"] = evidence[-MAX_HISTORY_ITEMS:]
    previous_merged = set(target_metadata.get("merged_sources", []))
    previous_merged.update(merged_ids)
    target_metadata["merged_sources"] = sorted(previous_merged)
    repo.update(target, access_count=access_count, metadata=target_metadata)
    write_audit(
        session,
        action="memory.merged",
        target_type="memory",
        target_id=target.id,
        workspace_id=target.workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        before=target_before,
        after={
            **_audit_snapshot(target),
            "merged_sources": merged_ids,
        },
    )
    emit_memory_event(session, target, "merged")
    session.commit()
    return _memory_payload(target)


@router.delete("")
def forget_memories(
    payload: ForgetRequest,
    context: AuthContext = Depends(require_write),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """按查询条件批量遗忘；默认软删，硬删必须 admin + confirm。

    批量遗忘必须能确定工作区：管理员会话显式传 ``workspace_id``，API Key
    省略时使用自身工作区；跨 workspace 的 Key 统一 404。
    """
    query = payload.query.strip()
    if not query:
        raise BadRequestError("query 不能为空", code="invalid_request")

    resolved = resolve_workspace_id(context, payload.workspace_id)
    ensure_workspace(session, context, resolved)

    if payload.hard and (
        not payload.confirm or not context.has_scope("admin")
    ):
        raise BadRequestError(
            "硬删除需要 admin 权限且 confirm=true",
            code="confirmation_required",
        )

    statement = select(Memory).where(
        Memory.workspace_id == resolved,
        Memory.deleted_at.is_(None),
        Memory.status != "deleted",
    )
    if payload.scope is not None:
        statement = statement.where(Memory.scope == payload.scope)
    if payload.scope_id is not None:
        statement = statement.where(Memory.scope_id == payload.scope_id)
    if payload.memory_type is not None:
        statement = statement.where(Memory.type == payload.memory_type)
    statement = statement.where(
        Memory.content.ilike(_contains_pattern(query), escape="\\")
    )
    statement = statement.order_by(Memory.created_at.asc(), Memory.id.asc())

    memories = list(session.scalars(statement))
    if not memories:
        return {"affected": 0, "ids": []}

    actor_type, actor_id = audit_actor(context)
    affected: list[str] = []
    cascades: list[tuple[str, list[str]]] = []
    for memory in memories:
        affected.append(memory.id)
        before = _audit_snapshot(memory)
        if payload.hard:
            memory_id_value, asset_paths = _mark_hard_deleted(
                session,
                memory,
                actor_type=actor_type,
                actor_id=actor_id,
                query=query,
            )
            cascades.append((memory_id_value, asset_paths))
            continue

        MemoryRepository(session).update(
            memory, status="deleted", deleted_at=utcnow()
        )
        write_audit(
            session,
            action="memory.deleted",
            target_type="memory",
            target_id=memory.id,
            workspace_id=memory.workspace_id,
            actor_type=actor_type,
            actor_id=actor_id,
            before=before,
            after={**_audit_snapshot(memory), "query": query},
        )
        emit_memory_event(session, memory, "deleted", deleted=True)

    session.commit()
    for memory_id_value, asset_paths in cascades:
        cascade_hard_delete(memory_id_value, asset_paths)
    return {"affected": len(affected), "ids": affected}


@router.get("/{memory_id}")
def get_memory(
    memory_id: str,
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    memory = _load_memory(session, context, memory_id)
    body = _memory_payload(memory)
    body["history"] = _history_payload(session, memory)
    body["links"] = _links_payload(session, memory)
    return body


@router.get("/{memory_id}/audit")
def get_memory_audit(
    memory_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """返回该记忆的审计轨迹，只含动作与变更字段名，不含快照原文。"""
    memory = _load_memory(session, context, memory_id)
    rows = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.target_type == "memory",
            AuditLog.target_id == memory.id,
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
    ).all()
    return {"items": [_audit_payload(entry) for entry in rows]}


@router.patch("/{memory_id}")
def patch_memory(
    memory_id: str,
    payload: MemoryPatch,
    context: AuthContext = Depends(require_write),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    memory = _load_memory(session, context, memory_id)
    fields = payload.model_fields_set
    if not fields:
        raise BadRequestError("没有可更新的字段", code="empty_patch")

    before = _audit_snapshot(memory)
    metadata = dict(memory.metadata_ or {})
    metadata_dirty = False
    updates: dict[str, Any] = {}

    if "content" in fields and payload.content is not None:
        content = normalize_content(payload.content)
        updates["content"] = content
        pii_types = detect_pii(content)
        if pii_types:
            metadata["pii"] = True
            metadata["pii_types"] = pii_types
            metadata["pii_fields"] = ["content"]
        else:
            for key in ("pii", "pii_types", "pii_fields"):
                metadata.pop(key, None)
        metadata_dirty = True

    if "summary" in fields:
        updates["summary"] = payload.summary
    if "importance" in fields and payload.importance is not None:
        updates["importance"] = payload.importance
    if "valid_to" in fields:
        updates["valid_to"] = payload.valid_to
    if "tags" in fields:
        metadata["tags"] = list(payload.tags or [])
        metadata_dirty = True

    if metadata_dirty:
        updates["metadata"] = metadata
    if not updates:
        raise BadRequestError("没有可更新的字段", code="empty_patch")

    MemoryRepository(session).update(memory, **updates)
    actor_type, actor_id = audit_actor(context)
    write_audit(
        session,
        action="memory.updated",
        target_type="memory",
        target_id=memory.id,
        workspace_id=memory.workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        before=before,
        after=_audit_snapshot(memory),
    )
    emit_memory_event(session, memory, "updated")
    session.commit()
    return _memory_payload(memory)


@router.delete("/{memory_id}")
def delete_memory(
    memory_id: str,
    hard: bool = Query(default=False),
    confirm: bool = Query(default=False),
    context: AuthContext = Depends(require_write),
    session: Session = Depends(get_db),
):
    memory = _load_memory(session, context, memory_id)
    actor_type, actor_id = audit_actor(context)
    before = _audit_snapshot(memory)

    if hard:
        if not confirm or not context.has_scope("admin"):
            raise BadRequestError(
                "硬删除需要 admin 权限且 confirm=true",
                code="confirmation_required",
            )
        memory_id_value, asset_paths = _mark_hard_deleted(
            session,
            memory,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        session.commit()
        cascade = cascade_hard_delete(memory_id_value, asset_paths)
        return {
            "id": memory_id_value,
            "deleted": True,
            "hard": True,
            "status": "purged",
            "cascade": cascade,
        }

    MemoryRepository(session).update(
        memory, status="deleted", deleted_at=utcnow()
    )
    write_audit(
        session,
        action="memory.deleted",
        target_type="memory",
        target_id=memory.id,
        workspace_id=memory.workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        before=before,
        after=_audit_snapshot(memory),
    )
    emit_memory_event(session, memory, "deleted", deleted=True)
    session.commit()
    return Response(status_code=204)


@router.post("/{memory_id}/pin")
def pin_memory(
    memory_id: str,
    context: AuthContext = Depends(require_write),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    memory = _load_memory(session, context, memory_id)
    before = _audit_snapshot(memory)
    metadata = dict(memory.metadata_ or {})
    metadata["pinned"] = True
    MemoryRepository(session).update(
        memory, importance=1.0, metadata=metadata
    )
    actor_type, actor_id = audit_actor(context)
    write_audit(
        session,
        action="memory.pinned",
        target_type="memory",
        target_id=memory.id,
        workspace_id=memory.workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        before=before,
        after=_audit_snapshot(memory),
    )
    emit_memory_event(session, memory, "pinned")
    session.commit()
    return _memory_payload(memory)


@router.post("/{memory_id}/feedback")
def memory_feedback(
    memory_id: str,
    payload: FeedbackRequest,
    context: AuthContext = Depends(require_write),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    memory = _load_memory(session, context, memory_id)
    feedback_memory(
        session,
        memory,
        helpful=payload.helpful,
        reason=payload.reason,
        actor=context,
    )
    return _memory_payload(memory)


__all__ = ["router"]
