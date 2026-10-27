"""Celery 应用与 beat 调度。"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

_settings = get_settings()

celery_app = Celery(
    "agent_memory",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=["app.jobs.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_default_queue="memory",
    beat_schedule={
        "dispatch-outbox": {
            "task": "app.jobs.tasks.dispatch_outbox",
            "schedule": 5.0,
        },
        "heal-projection-identity": {
            "task": "app.jobs.tasks.heal_projection_identity",
            "schedule": 600.0,
        },
        # 会话结束/空闲 30 分钟：每 10 分钟扫描一次，任务内部再按 30 分钟过滤。
        "consolidate-idle-sessions": {
            "task": "app.jobs.tasks.consolidate_idle_sessions",
            "schedule": 600.0,
        },
        # 夜间批量巩固：默认 03:00（UTC），可由配置覆盖。
        "consolidate-nightly": {
            "task": "app.jobs.tasks.consolidate_nightly",
            "schedule": crontab(
                hour=_settings.consolidation_nightly_hour,
                minute=_settings.consolidation_nightly_minute,
            ),
        },
    },
)


__all__ = ["celery_app"]
