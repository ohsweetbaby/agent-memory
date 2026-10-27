"""Transactional Outbox 仓储。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import OutboxEvent


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

    def claim_batch(self, limit: int = 50) -> list[OutboxEvent]:
        """抢占一批到期事件。

        使用 ``FOR UPDATE SKIP LOCKED`` 避免多 worker 重复消费；事务由调用方
        提交，未提交时行锁一直持有，天然保证「领取 → 处理」的原子边界。
        """
        stmt = (
            select(OutboxEvent)
            .where(
                OutboxEvent.status == "pending",
                OutboxEvent.next_attempt_at <= func.now(),
            )
            .order_by(OutboxEvent.next_attempt_at, OutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        events = list(self.session.scalars(stmt))
        for event in events:
            event.status = "processing"
            event.attempts += 1
        self.session.flush()
        return events
