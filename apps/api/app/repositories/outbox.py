"""Transactional Outbox 仓储。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.models import OutboxEvent

#: 领取事件的租约；worker 异常退出后，过期事件会被其他 worker 回收。
DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 8


class OutboxRepository:
    """在业务事务内写事件，由 worker 抢占并投影到外部索引。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        event_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        *,
        aggregate_type: str = "memory",
    ) -> OutboxEvent:
        event = OutboxEvent(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def claim_batch(
        self,
        limit: int = 50,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> list[OutboxEvent]:
        """抢占一批到期事件。

        - ``pending``：``next_attempt_at`` 已到期即可领取。
        - ``processing``：超过租约（``next_attempt_at < now``）视为上一个
          worker 已崩溃，可回收重试。
        - 达到 ``max_attempts`` 的事件不再领取，由 :meth:`dead_letter_exhausted`
          标记为 ``dead``。

        使用 ``FOR UPDATE SKIP LOCKED`` 避免多 worker 重复消费；事务由调用方
        提交，未提交时行锁一直持有，天然保证「领取 → 处理」的原子边界。
        """
        now = datetime.now(UTC)
        stmt = (
            select(OutboxEvent)
            .where(
                OutboxEvent.attempts < max_attempts,
                or_(
                    and_(
                        OutboxEvent.status == "pending",
                        OutboxEvent.next_attempt_at <= now,
                    ),
                    and_(
                        OutboxEvent.status == "processing",
                        OutboxEvent.next_attempt_at <= now,
                    ),
                ),
            )
            .order_by(OutboxEvent.next_attempt_at, OutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        events = list(self.session.scalars(stmt))
        lease_until = now + timedelta(seconds=lease_seconds)
        for event in events:
            event.status = "processing"
            event.attempts += 1
            event.next_attempt_at = lease_until
        self.session.flush()
        return events

    def dead_letter_exhausted(
        self, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS
    ) -> int:
        """把重试次数耗尽的事件标记为死信，返回本次数量。"""
        stmt = (
            update(OutboxEvent)
            .where(
                OutboxEvent.status.in_(("pending", "processing")),
                OutboxEvent.attempts >= max_attempts,
            )
            .values(status="dead")
        )
        result = self.session.execute(stmt)
        self.session.flush()
        return int(result.rowcount or 0)


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "OutboxRepository",
]
