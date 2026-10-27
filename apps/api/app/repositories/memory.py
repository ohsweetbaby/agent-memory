"""记忆仓储：所有查询强制按 workspace 隔离。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import Memory

# 可被仓储更新的字段；id / workspace_id / version 等由系统维护。
_UPDATABLE_FIELDS = frozenset(
    {
        "scope",
        "scope_id",
        "type",
        "content",
        "summary",
        "importance",
        "confidence",
        "stability",
        "access_count",
        "last_reinforced_at",
        "last_accessed_at",
        "valid_from",
        "valid_to",
        "superseded_by",
        "status",
        "source_type",
        "source_id",
        "metadata_",
        "consolidation_run_id",
        "deleted_at",
    }
)


@dataclass(slots=True)
class MemoryFilters:
    """列表查询的收窄条件；缺省字段不参与过滤。"""

    scope: str | None = None
    scope_id: str | None = None
    type: str | None = None
    status: str | None = None
    include_deleted: bool = False
    valid_at: datetime | None = None


def _scope_value(scope: str | Enum) -> str:
    """兼容领域层的 ``Scope`` 枚举与普通字符串。"""
    return scope.value if isinstance(scope, Enum) else str(scope)


class MemoryRepository:
    """``memory`` 表的读写入口；调用方负责提交事务。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, memory: Memory) -> Memory:
        self.session.add(memory)
        self.session.flush()
        return memory

    def get(self, workspace_id: str, memory_id: str) -> Memory | None:
        """按 (workspace, id) 精确读取；跨 workspace 一律返回 None。"""
        stmt = select(Memory).where(
            Memory.workspace_id == workspace_id,
            Memory.id == memory_id,
        )
        return self.session.scalar(stmt)

    def list(
        self, workspace_id: str, filters: MemoryFilters | None = None
    ) -> list[Memory]:
        resolved = filters or MemoryFilters()
        stmt: Select[tuple[Memory]] = select(Memory).where(
            Memory.workspace_id == workspace_id
        )
        if not resolved.include_deleted:
            stmt = stmt.where(Memory.deleted_at.is_(None))
        if resolved.scope is not None:
            stmt = stmt.where(Memory.scope == resolved.scope)
        if resolved.scope_id is not None:
            stmt = stmt.where(Memory.scope_id == resolved.scope_id)
        if resolved.type is not None:
            stmt = stmt.where(Memory.type == resolved.type)
        if resolved.status is not None:
            stmt = stmt.where(Memory.status == resolved.status)
        if resolved.valid_at is not None:
            stmt = stmt.where(
                Memory.valid_from <= resolved.valid_at,
                or_(Memory.valid_to.is_(None), Memory.valid_to > resolved.valid_at),
            )
        stmt = stmt.order_by(Memory.created_at.desc(), Memory.id.desc())
        return list(self.session.scalars(stmt))

    def update(self, memory: Memory, **fields: Any) -> Memory:
        """更新白名单字段，并让 ``version`` 自增以满足投影幂等。"""
        unknown = set(fields) - _UPDATABLE_FIELDS - {"metadata"}
        if unknown:
            raise ValueError(f"不可更新的字段: {', '.join(sorted(unknown))}")

        for name, value in fields.items():
            attribute = "metadata_" if name == "metadata" else name
            setattr(memory, attribute, value)
        memory.version += 1
        self.session.flush()
        return memory

    def find_similar(
        self,
        workspace_id: str,
        scope_pairs: Sequence[tuple[str | Enum, str | None]],
        content: str,
        limit: int = 3,
    ) -> list[tuple[Memory, float]]:
        """pg_trgm 相似度检索，作用域与 workspace 同时收窄。

        返回 ``(memory, similarity)``，按相似度降序；仅考虑 active 且当前
        有效、未删除的记忆，供去重与冲突判定使用。
        """
        if not scope_pairs or not content or not content.strip():
            return []

        scope_conditions = []
        for scope, scope_id in scope_pairs:
            normalized = _scope_value(scope)
            if scope_id is None or not str(scope_id).strip():
                scope_conditions.append(
                    and_(Memory.scope == normalized, Memory.scope_id.is_(None))
                )
            else:
                scope_conditions.append(
                    and_(
                        Memory.scope == normalized,
                        Memory.scope_id == str(scope_id),
                    )
                )
        if not scope_conditions:
            return []

        score = func.similarity(Memory.content, content).label("similarity")
        stmt = (
            select(Memory, score)
            .where(
                Memory.workspace_id == workspace_id,
                or_(*scope_conditions),
                Memory.status == "active",
                Memory.deleted_at.is_(None),
                or_(Memory.valid_to.is_(None), Memory.valid_to > func.now()),
                score > 0,
            )
            .order_by(score.desc(), Memory.importance.desc(), Memory.id.desc())
            .limit(limit)
        )
        rows = self.session.execute(stmt).all()
        return [(row[0], float(row[1])) for row in rows]
