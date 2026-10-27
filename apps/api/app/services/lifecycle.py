"""生命周期维护：遗忘扫描、软删清理、运维历史裁剪与索引对账。

规格 §5.4/§10.4/§11/§12 对这里的约束：

- 遗忘只处理 ``status=active``：保留率低于阈值时仅改状态为 ``archived``，
  写 ``metadata.archived_reason="low_retention"``，绝不物理删除；
- 清理只处理 ``status=deleted`` 且 ``deleted_at`` 超过保留期的行，置为
  ``purged`` 并写 outbox；worker 再删除 Qdrant/Neo4j 投影和
  ``metadata.asset_paths`` 指向的文件（对账任务兜底孤儿）；
- 对账抽样检查 active/archived 等未删除记忆的投影，修复缺失与过期版本；
- 所有查询都带 ``workspace_id``；跨 workspace 的批量任务由调用方逐区执行。

服务函数只 ``flush``，事务提交交给 API/worker，便于与审计、outbox 同事务。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from memory_core.decay import DecayParams, should_archive

from app.adapters.neo4j import GraphEntity, GraphLink
from app.models import AuditLog, Entity, Memory, MemoryEntity, MemoryLink, ReviewItem
from app.repositories.embedding import EmbeddingRepository
from app.repositories.memory import MemoryRepository
from app.repositories.outbox import OutboxRepository
from app.services.audit import write_audit

logger = logging.getLogger(__name__)

REMOVED_STATUSES = frozenset({"deleted", "purged"})
DEFAULT_PURGE_RETENTION_DAYS = 30
DEFAULT_AUDIT_RETENTION_DAYS = 180
DEFAULT_REVIEW_RETENTION_DAYS = 90


# ---------------------------------------------------------------------------
# 遗忘与清理
# ---------------------------------------------------------------------------


def _active_memories(
    db: Session, workspace_id: str | None
) -> list[Memory]:
    statement = select(Memory).where(
        Memory.status == "active",
        Memory.deleted_at.is_(None),
    )
    if workspace_id is not None:
        statement = statement.where(Memory.workspace_id == workspace_id)
    return list(db.scalars(statement.order_by(Memory.created_at.asc(), Memory.id.asc())))


def decay_sweep(
    db: Session,
    now: datetime,
    params: DecayParams,
    *,
    workspace_id: str | None = None,
) -> int:
    """按 Ebbinghaus 保留率归档低价值记忆，返回归档条数。

    只处理 ``active``；重复运行不会再次处理已归档行。非 active 状态、已软删
    或已清除的行保持不变。
    """
    repo = MemoryRepository(db)
    archived = 0
    for memory in _active_memories(db, workspace_id):
        if not should_archive(
            memory.last_reinforced_at,
            now,
            float(memory.importance or 0.0),
            int(memory.access_count or 0),
            float(memory.stability or 1.0),
            params,
        ):
            continue

        before = {
            "status": memory.status,
            "metadata": dict(memory.metadata_ or {}),
        }
        metadata = dict(memory.metadata_ or {})
        metadata["archived_reason"] = "low_retention"
        metadata["archived_at"] = now.isoformat()
        repo.update(memory, status="archived", metadata=metadata)
        # 投影 payload 也带 status；归档后补一条 upsert 让索引与事实源同步。
        OutboxRepository(db).add(
            "memory.upserted",
            memory.id,
            {
                "memory_id": memory.id,
                "version": memory.version,
                "workspace_id": memory.workspace_id,
                "action": "archived",
                "status": memory.status,
                "type": memory.type,
                "scope": memory.scope,
                "scope_id": memory.scope_id,
            },
        )
        write_audit(
            db,
            action="decay.archived",
            target_type="memory",
            target_id=memory.id,
            workspace_id=memory.workspace_id,
            actor_type="system",
            before=before,
            after={"status": "archived", "metadata": metadata},
        )
        archived += 1

    if archived:
        db.flush()
        logger.info(
            "decay sweep archived memories",
            extra={"count": archived, "workspace_id": workspace_id},
        )
    return archived


def _purgeable_memories(
    db: Session, cutoff: datetime, workspace_id: str | None
) -> list[Memory]:
    statement = select(Memory).where(
        Memory.status == "deleted",
        Memory.deleted_at.is_not(None),
        Memory.deleted_at < cutoff,
    )
    if workspace_id is not None:
        statement = statement.where(Memory.workspace_id == workspace_id)
    return list(
        db.scalars(statement.order_by(Memory.deleted_at.asc(), Memory.id.asc()))
    )


def purge_deleted(
    db: Session,
    now: datetime,
    retention_days: int = DEFAULT_PURGE_RETENTION_DAYS,
    *,
    workspace_id: str | None = None,
) -> int:
    """把超过保留期的软删记忆置为 ``purged``，并写硬删除 outbox。

    行本身作为墓碑保留（供审计/对账追溯），外部索引与资产由 worker 清理。
    重复执行幂等：``deleted`` 状态才会被选中。
    """
    days = max(int(retention_days), 0)
    cutoff = now - timedelta(days=days)
    repo = MemoryRepository(db)
    outbox = OutboxRepository(db)
    purged = 0
    for memory in _purgeable_memories(db, cutoff, workspace_id):
        asset_paths = list((memory.metadata_ or {}).get("asset_paths") or [])
        metadata = dict(memory.metadata_ or {})
        metadata["purged_at"] = now.isoformat()
        before = {
            "status": memory.status,
            "deleted_at": memory.deleted_at.isoformat()
            if memory.deleted_at
            else None,
            "metadata": dict(memory.metadata_ or {}),
        }
        repo.update(memory, status="purged", metadata=metadata)
        write_audit(
            db,
            action="purge.completed",
            target_type="memory",
            target_id=memory.id,
            workspace_id=memory.workspace_id,
            actor_type="system",
            before=before,
            after={
                "status": "purged",
                "asset_paths": asset_paths,
                "retention_days": days,
            },
        )
        outbox.add(
            "memory.deleted",
            memory.id,
            {
                "memory_id": memory.id,
                "version": memory.version,
                "workspace_id": memory.workspace_id,
                "action": "purged",
                "hard": True,
                "asset_paths": asset_paths,
            },
        )
        purged += 1

    if purged:
        db.flush()
        logger.info(
            "soft-deleted memories purged",
            extra={"count": purged, "workspace_id": workspace_id},
        )
    return purged


def prune_ops_history(
    db: Session,
    now: datetime,
    audit_days: int = DEFAULT_AUDIT_RETENTION_DAYS,
    review_days: int = DEFAULT_REVIEW_RETENTION_DAYS,
    *,
    workspace_id: str | None = None,
) -> int:
    """裁剪过期审计与待审项，返回删除总数。

    审计保留 180 天、待审项保留 90 天（均可配置）。审计只追加，因此这里只
    删除早已超过保留期的历史行，不修改任何业务数据。
    """
    audit_cutoff = now - timedelta(days=max(int(audit_days), 0))
    review_cutoff = now - timedelta(days=max(int(review_days), 0))

    audit_statement = delete(AuditLog).where(AuditLog.created_at < audit_cutoff)
    review_statement = delete(ReviewItem).where(
        ReviewItem.created_at < review_cutoff
    )
    if workspace_id is not None:
        # 系统级审计的 workspace_id 为 NULL；逐区清理时也必须覆盖，否则
        # 登录/初始化等全局审计永远不会过期。首个 workspace 处理掉后其余为 0。
        audit_statement = audit_statement.where(
            or_(
                AuditLog.workspace_id == workspace_id,
                AuditLog.workspace_id.is_(None),
            )
        )
        review_statement = review_statement.where(
            ReviewItem.workspace_id == workspace_id
        )

    audit_removed = int(db.execute(audit_statement).rowcount or 0)
    review_removed = int(db.execute(review_statement).rowcount or 0)
    total = audit_removed + review_removed
    if total:
        db.flush()
        logger.info(
            "ops history pruned",
            extra={
                "audit_removed": audit_removed,
                "review_removed": review_removed,
                "workspace_id": workspace_id,
            },
        )
    return total


# ---------------------------------------------------------------------------
# 资产与投影清理
# ---------------------------------------------------------------------------


def delete_asset_paths(paths: Iterable[Any] | None) -> int:
    """删除 ``metadata.asset_paths`` 指向的普通文件；不存在时跳过。

    M1 没有独立资产表，路径是内部写入的绝对路径；M3 上线后由资产表接管。
    删除动作幂等，重复执行不会报错。
    """
    removed = 0
    for raw in paths or ():
        if not raw:
            continue
        try:
            path = Path(str(raw)).expanduser()
            if not path.is_file() and not path.is_symlink():
                continue
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            logger.warning("failed to delete asset file", extra={"path": str(raw)})
    return removed


def memory_asset_paths(memory: Memory | None) -> list[str]:
    if memory is None:
        return []
    return [str(value) for value in (memory.metadata_ or {}).get("asset_paths") or []]


def cascade_hard_delete(
    memory_id: str, asset_paths: Iterable[Any] | None
) -> dict[str, int | bool]:
    """尽力立即清理外部索引与资产，返回清理清单。

    API 请求线程只有在测试/嵌入式调用显式注入运行时时才同步访问 Qdrant/
    Neo4j；生产环境由 outbox worker 执行同一动作。本地文件删除始终尝试，
    失败时保留 outbox 供 worker 重试。所有动作幂等。
    """
    result: dict[str, int | bool] = {
        "qdrant": False,
        "neo4j": False,
        "assets": delete_asset_paths(asset_paths),
    }
    from app.jobs.runtime import peek_runtime

    runtime = peek_runtime()
    if runtime is None:
        return result
    try:
        runtime.qdrant.delete(memory_id)
        result["qdrant"] = True
    except Exception:
        logger.warning(
            "immediate qdrant cascade failed; worker will retry",
            extra={"memory_id": memory_id},
        )
    if runtime.neo4j is not None:
        try:
            runtime.neo4j.delete(memory_id)
            result["neo4j"] = True
        except Exception:
            logger.warning(
                "immediate neo4j cascade failed; worker will retry",
                extra={"memory_id": memory_id},
            )
    return result


# ---------------------------------------------------------------------------
# 索引对账
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReconcileReport:
    """一次对账的结果；``missing_after`` 是修复后仍缺失的记忆数。"""

    checked: int = 0
    missing: int = 0
    stale: int = 0
    repaired: int = 0
    failed: int = 0
    missing_after: int = 0
    orphans_removed: int = 0
    workspaces: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "missing": self.missing,
            "stale": self.stale,
            "repaired": self.repaired,
            "failed": self.failed,
            "missing_after": self.missing_after,
            "orphans_removed": self.orphans_removed,
            "workspaces": self.workspaces,
        }


def _qdrant_identity(qdrant: Any) -> str:
    provider = getattr(qdrant, "provider", "unknown")
    model = getattr(qdrant, "model", "unknown")
    return f"{provider}:{model}"


def _fallback_vector(memory: Memory, dim: int) -> list[float]:
    """没有嵌入缓存时的确定性兜底向量，保证对账能先补点。"""
    digest = hashlib.sha256(
        f"{memory.id}:{memory.content}".encode("utf-8")
    ).digest()
    raw = (digest * ((dim * 4) // len(digest) + 1))[: dim * 4]
    values = [
        int.from_bytes(raw[index : index + 4], "big") / 2**31 - 1.0
        for index in range(0, dim * 4, 4)
    ]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def _memory_vector(db: Session, memory: Memory, qdrant: Any) -> list[float]:
    dim = int(getattr(qdrant, "dim", 0) or 0)
    if dim <= 0:
        raise ValueError("qdrant 缺少有效维度")
    repo = EmbeddingRepository(db)
    cached = repo.get(memory.id, _qdrant_identity(qdrant))
    if cached is not None and len(cached) == dim:
        return cached

    # worker 里有真实嵌入 Provider 时优先重算，避免长期使用兜底向量。
    from app.jobs.runtime import peek_runtime

    runtime = peek_runtime()
    if runtime is not None and runtime.embedding is not None:
        try:
            cached = repo.get(memory.id, runtime.embedding.identity)
            if cached is None or len(cached) != dim:
                generated = asyncio.run(
                    runtime.embedding.embed([memory.content])
                )[0]
                if len(generated) == dim:
                    repo.upsert(
                        memory.id, runtime.embedding.identity, dim, generated
                    )
                    return list(generated)
            else:
                return list(cached)
        except Exception:  # 嵌入失败不能阻止索引补点
            logger.warning(
                "reconcile embedding failed; using deterministic vector",
                extra={"memory_id": memory.id},
            )
    return _fallback_vector(memory, dim)


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


def _qdrant_payload(db: Session, memory: Memory) -> dict[str, Any]:
    entity_ids = [
        str(value)
        for value in db.scalars(
            select(MemoryEntity.entity_id)
            .where(MemoryEntity.memory_id == memory.id)
            .order_by(MemoryEntity.entity_id)
        )
    ]
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


def _repair_qdrant(
    db: Session, memory: Memory, qdrant: Any
) -> bool:
    try:
        qdrant.upsert(
            memory.id,
            _memory_vector(db, memory, qdrant),
            _qdrant_payload(db, memory),
        )
    except Exception:
        logger.exception(
            "qdrant reconcile repair failed", extra={"memory_id": memory.id}
        )
        return False
    try:
        from app.jobs.tasks import mark_projected

        target = f"qdrant:{getattr(qdrant, 'collection', 'unknown')}"
        mark_projected(db, memory.id, target, memory.version)
    except Exception:
        logger.warning(
            "failed to record reconciled projection state",
            extra={"memory_id": memory.id},
        )
    return True


def _repair_neo4j(db: Session, memory: Memory, neo4j: Any) -> bool:
    try:
        neo4j.upsert_memory(
            memory, _entity_refs(db, memory.id), _links(db, memory.id)
        )
    except Exception:
        logger.exception(
            "neo4j reconcile repair failed", extra={"memory_id": memory.id}
        )
        return False
    try:
        from app.jobs.tasks import mark_projected

        mark_projected(db, memory.id, "neo4j", memory.version)
    except Exception:
        logger.warning(
            "failed to record reconciled graph state",
            extra={"memory_id": memory.id},
        )
    return True


def _needs_qdrant(memory: Memory, qdrant: Any) -> tuple[bool, bool]:
    """返回 ``(缺失, 过期)``；外部异常按缺失处理。"""
    try:
        payload = qdrant.get(memory.id)
    except Exception:
        logger.exception(
            "qdrant reconcile check failed", extra={"memory_id": memory.id}
        )
        return True, False
    if payload is None:
        return True, False
    return False, int(payload.get("version") or 0) != int(memory.version)


def _needs_neo4j(memory: Memory, neo4j: Any) -> tuple[bool, bool]:
    if neo4j is None:
        return False, False
    try:
        node = neo4j.get(memory.id)
    except Exception:
        logger.exception(
            "neo4j reconcile check failed", extra={"memory_id": memory.id}
        )
        return True, False
    if node is None:
        return True, False
    return False, node.get("status") != memory.status


def _workspace_ids_with_memories(db: Session) -> list[str]:
    """包含已删除/已 purged 的 workspace，便于清理遗留孤儿投影。"""
    return [
        str(value)
        for value in db.scalars(
            select(Memory.workspace_id)
            .distinct()
            .order_by(Memory.workspace_id)
        )
    ]


def _reconcile_workspace(
    db: Session,
    qdrant: Any,
    neo4j: Any,
    workspace_id: str,
    sample: int,
    *,
    include_orphans: bool = True,
) -> dict[str, int]:
    statement = (
        select(Memory)
        .where(
            Memory.workspace_id == workspace_id,
            Memory.status.not_in(tuple(REMOVED_STATUSES)),
            Memory.deleted_at.is_(None),
        )
        .order_by(Memory.updated_at.desc(), Memory.id.desc())
        .limit(sample)
    )
    memories = list(db.scalars(statement))
    counts = {
        "checked": len(memories),
        "missing": 0,
        "stale": 0,
        "repaired": 0,
        "failed": 0,
        "missing_after": 0,
        "orphans_removed": 0,
    }

    for memory in memories:
        qdrant_missing, qdrant_stale = _needs_qdrant(memory, qdrant)
        neo4j_missing, neo4j_stale = _needs_neo4j(memory, neo4j)
        missing = qdrant_missing or neo4j_missing
        stale = qdrant_stale or neo4j_stale
        if missing:
            counts["missing"] += 1
        elif stale:
            counts["stale"] += 1
        if not (missing or stale):
            continue

        ok = True
        if qdrant_missing or qdrant_stale:
            ok = _repair_qdrant(db, memory, qdrant) and ok
        if neo4j_missing or neo4j_stale:
            ok = _repair_neo4j(db, memory, neo4j) and ok
        if ok:
            counts["repaired"] += 1
        else:
            counts["failed"] += 1

    # 修复后复核；同时清理物理已删除记忆遗留在 Qdrant 的孤儿 point。
    for memory in memories:
        qdrant_missing, _ = _needs_qdrant(memory, qdrant)
        neo4j_missing, _ = _needs_neo4j(memory, neo4j)
        if qdrant_missing or neo4j_missing:
            counts["missing_after"] += 1

    list_ids = getattr(qdrant, "list_ids", None)
    delete_many = getattr(qdrant, "delete_many", None)
    if include_orphans and callable(list_ids) and callable(delete_many):
        try:
            existing = set(list_ids(workspace_id))
            known = set(
                db.scalars(
                    select(Memory.id).where(
                        Memory.workspace_id == workspace_id,
                        Memory.status.not_in(tuple(REMOVED_STATUSES)),
                        Memory.deleted_at.is_(None),
                    )
                )
            )
            orphans = sorted(existing - known)
            if orphans:
                delete_many(orphans)
                counts["orphans_removed"] += len(orphans)
        except Exception:
            logger.exception(
                "qdrant orphan cleanup failed",
                extra={"workspace_id": workspace_id},
            )

    return counts


def reconcile_indexes(
    db: Session,
    qdrant: Any,
    neo4j: Any,
    sample: int = 100,
    *,
    workspace_id: str | None = None,
    include_orphans: bool = True,
) -> ReconcileReport:
    """抽样对账并修复缺失/过期索引。

    ``workspace_id=None`` 时显式逐 workspace 执行，避免跨租户混查；每个区
    独立抽样 ``sample`` 条。返回聚合报告，``workspaces`` 记录各区计数。
    """
    if sample <= 0:
        raise ValueError("sample 必须大于 0")
    scope = [workspace_id] if workspace_id is not None else _workspace_ids_with_memories(db)
    report = ReconcileReport()
    for current in scope:
        counts = _reconcile_workspace(
            db,
            qdrant,
            neo4j,
            current,
            sample,
            include_orphans=include_orphans,
        )
        report.checked += counts["checked"]
        report.missing += counts["missing"]
        report.stale += counts["stale"]
        report.repaired += counts["repaired"]
        report.failed += counts["failed"]
        report.missing_after += counts["missing_after"]
        report.orphans_removed += counts["orphans_removed"]
        report.workspaces[current] = counts
        if counts["checked"]:
            db.flush()
    if report.checked or report.orphans_removed:
        logger.info(
            "index reconciliation finished",
            extra={
                "checked": report.checked,
                "repaired": report.repaired,
                "missing_after": report.missing_after,
                "orphans_removed": report.orphans_removed,
            },
        )
    return report


__all__ = [
    "REMOVED_STATUSES",
    "ReconcileReport",
    "cascade_hard_delete",
    "decay_sweep",
    "delete_asset_paths",
    "memory_asset_paths",
    "prune_ops_history",
    "purge_deleted",
    "reconcile_indexes",
]
