"""管理台统计聚合。

所有数字都直接来自数据库；``workspace_id`` 为 ``None`` 时表示管理员会话
跨工作区查看，API Key 由路由层固定为自身工作区。

统计只排除软删除行，``archived`` / ``superseded`` 仍计入记忆总量，因为
它们仍是事实源的一部分；``active_memories`` 单独给出当前 active 数量。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Entity, Episode, Memory, ReviewItem
from app.models import Session as SessionModel
from app.models.base import utcnow

MEMORY_TYPES = ("working", "episodic", "semantic", "procedural", "perceptual")
MEMORY_SCOPES = ("global", "agent", "user", "session")
MEMORY_STATUSES = ("active", "archived", "superseded", "purged")


@dataclass(frozen=True, slots=True)
class Overview:
    """概览页所需的计数。"""

    total_memories: int
    active_memories: int
    by_type: dict[str, int]
    by_scope: dict[str, int]
    by_status: dict[str, int]
    pending_review: int
    total_episodes: int
    total_entities: int
    created_last_30_days: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_memories": self.total_memories,
            "active_memories": self.active_memories,
            "by_type": self.by_type,
            "by_scope": self.by_scope,
            "by_status": self.by_status,
            "pending_review": self.pending_review,
            "total_episodes": self.total_episodes,
            "total_entities": self.total_entities,
            "created_last_30_days": self.created_last_30_days,
        }


@dataclass(frozen=True, slots=True)
class Point:
    """按天增长曲线上的一个点。"""

    date: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "count": self.count}


def _memory_conditions(workspace_id: str | None) -> list[Any]:
    conditions: list[Any] = [
        Memory.deleted_at.is_(None),
        Memory.status != "deleted",
    ]
    if workspace_id is not None:
        conditions.append(Memory.workspace_id == workspace_id)
    return conditions


def _count(session: Session, statement: Any) -> int:
    return int(session.scalar(statement) or 0)


def _group_counts(
    session: Session,
    column: Any,
    conditions: list[Any],
    defaults: tuple[str, ...],
) -> dict[str, int]:
    result = {key: 0 for key in defaults}
    rows = session.execute(
        select(column, func.count()).where(*conditions).group_by(column)
    ).all()
    for key, count in rows:
        result[str(key)] = int(count)
    return result


def overview(session: Session, workspace_id: str | None) -> Overview:
    """返回指定工作区（或全部工作区）的概览计数。"""
    conditions = _memory_conditions(workspace_id)

    total = _count(
        session,
        select(func.count()).select_from(Memory).where(*conditions),
    )
    active = _count(
        session,
        select(func.count())
        .select_from(Memory)
        .where(*conditions, Memory.status == "active"),
    )
    by_type = _group_counts(
        session, Memory.type, conditions, MEMORY_TYPES
    )
    by_scope = _group_counts(
        session, Memory.scope, conditions, MEMORY_SCOPES
    )
    by_status = _group_counts(
        session, Memory.status, conditions, MEMORY_STATUSES
    )

    review_conditions: list[Any] = [ReviewItem.status == "pending"]
    if workspace_id is not None:
        review_conditions.append(ReviewItem.workspace_id == workspace_id)
    pending_review = _count(
        session,
        select(func.count())
        .select_from(ReviewItem)
        .where(*review_conditions),
    )

    episode_statement = select(func.count()).select_from(Episode).join(
        SessionModel, Episode.session_id == SessionModel.id
    )
    if workspace_id is not None:
        episode_statement = episode_statement.where(
            SessionModel.workspace_id == workspace_id
        )
    total_episodes = _count(session, episode_statement)

    entity_statement = select(func.count()).select_from(Entity)
    if workspace_id is not None:
        entity_statement = entity_statement.where(
            Entity.workspace_id == workspace_id
        )
    total_entities = _count(session, entity_statement)

    since = utcnow() - timedelta(days=30)
    created_last_30_days = _count(
        session,
        select(func.count())
        .select_from(Memory)
        .where(*conditions, Memory.created_at >= since),
    )

    return Overview(
        total_memories=total,
        active_memories=active,
        by_type=by_type,
        by_scope=by_scope,
        by_status=by_status,
        pending_review=pending_review,
        total_episodes=total_episodes,
        total_entities=total_entities,
        created_last_30_days=created_last_30_days,
    )


def timeseries(
    session: Session, workspace_id: str | None, days: int = 30
) -> list[Point]:
    """按 UTC 日期返回最近 ``days`` 天的新增记忆数（含零值日）。"""
    if days < 1 or days > 365:
        raise ValueError("days 必须在 1..365 之间")

    today = utcnow().date()
    start_date = today - timedelta(days=days - 1)
    start_at = datetime.combine(start_date, time.min, tzinfo=UTC)

    conditions = _memory_conditions(workspace_id)
    conditions.append(Memory.created_at >= start_at)
    day = func.date_trunc("day", Memory.created_at).label("day")
    rows = session.execute(
        select(day, func.count()).where(*conditions).group_by(day)
    ).all()

    counts: dict[Any, int] = {}
    for value, count in rows:
        key = value.date() if isinstance(value, datetime) else value
        counts[key] = int(count)

    return [
        Point(
            date=(start_date + timedelta(days=offset)).isoformat(),
            count=counts.get(start_date + timedelta(days=offset), 0),
        )
        for offset in range(days)
    ]


__all__ = ["Overview", "Point", "overview", "timeseries"]
