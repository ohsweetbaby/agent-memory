"""Celery beat 调度表（规格 §11）。

只集中声明时间，不注册任务实现；``celery_app`` 在启动时调用
:func:`build_beat_schedule`。每日 03:00 巩固、04:00 遗忘、05:00 清理，
索引对账每小时一次。
"""

from __future__ import annotations

from typing import Any

from celery.schedules import crontab

from app.config import Settings, get_settings


def build_beat_schedule(settings: Settings | None = None) -> dict[str, Any]:
    resolved = settings or get_settings()
    return {
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
                hour=resolved.consolidation_nightly_hour,
                minute=resolved.consolidation_nightly_minute,
            ),
        },
        # 遗忘扫描：只归档，不删除数据。
        "decay-sweep": {
            "task": "app.jobs.tasks.decay_sweep",
            "schedule": crontab(hour=resolved.decay_sweep_hour, minute=0),
        },
        # 软删保留期清理与运维历史裁剪同一时刻执行，互不依赖。
        "purge-deleted": {
            "task": "app.jobs.tasks.purge_deleted",
            "schedule": crontab(hour=resolved.purge_deleted_hour, minute=0),
        },
        "prune-ops-history": {
            "task": "app.jobs.tasks.prune_ops_history",
            "schedule": crontab(hour=resolved.purge_deleted_hour, minute=0),
        },
        # 索引对账：每小时抽样 100 条；每日全量重建由运维 CLI 负责。
        "reconcile-indexes": {
            "task": "app.jobs.tasks.reconcile_indexes",
            "schedule": crontab(minute=0),
        },
    }


__all__ = ["build_beat_schedule"]
