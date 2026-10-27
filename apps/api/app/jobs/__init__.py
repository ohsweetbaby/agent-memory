"""Celery worker：outbox 分发、嵌入与索引投影。"""

from __future__ import annotations

from app.jobs.celery_app import celery_app

__all__ = ["celery_app"]
