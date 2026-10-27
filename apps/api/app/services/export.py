"""工作区导出（规格 §11/§12）。

JSON 与 Markdown 都流式写入临时文件，避免一次性把整个工作区装进内存。
导出只包含 ``workspace / memories / episodes / audit``，不查询 API Key、
Provider 配置或任何密文；审计 ``before/after`` 还会递归剔除敏感字段名。
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_current_settings
from app.models import AuditLog, Episode, JobRun, Memory, Workspace
from app.models import Session as SessionModel

logger = logging.getLogger(__name__)

ExportFormat = Literal["json", "md"]
EXPORT_FORMATS = frozenset({"json", "md"})

_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "key_hash",
    "encrypted",
    "token",
    "api_key",
    "authorization",
    "cookie",
)


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _scrub(value: Any) -> Any:
    """递归剔除敏感字段名；只处理键，不改变普通正文。"""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                continue
            result[str(key)] = _scrub(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    return _iso(value)


def _workspace_record(workspace: Workspace) -> dict[str, Any]:
    return {
        "id": workspace.id,
        "name": workspace.name,
        "created_at": _iso(workspace.created_at),
    }


def _memory_record(memory: Memory) -> dict[str, Any]:
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
        "source_type": memory.source_type,
        "source_id": memory.source_id,
        "metadata": _scrub(dict(memory.metadata_ or {})),
        "valid_from": _iso(memory.valid_from),
        "valid_to": _iso(memory.valid_to),
        "superseded_by": memory.superseded_by,
        "last_reinforced_at": _iso(memory.last_reinforced_at),
        "last_accessed_at": _iso(memory.last_accessed_at),
        "deleted_at": _iso(memory.deleted_at),
        "created_at": _iso(memory.created_at),
        "updated_at": _iso(memory.updated_at),
    }


def _episode_record(episode: Episode) -> dict[str, Any]:
    return {
        "id": episode.id,
        "session_id": episode.session_id,
        "title": episode.title,
        "summary": episode.summary,
        "started_at": _iso(episode.started_at),
        "ended_at": _iso(episode.ended_at),
        "outcome": episode.outcome,
        "metadata": _scrub(dict(episode.metadata_ or {})),
        "created_at": _iso(episode.created_at),
    }


def _audit_record(entry: AuditLog) -> dict[str, Any]:
    return {
        "id": entry.id,
        "workspace_id": entry.workspace_id,
        "actor_type": entry.actor_type,
        "actor_id": entry.actor_id,
        "action": entry.action,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "before": _scrub(entry.before),
        "after": _scrub(entry.after),
        "created_at": _iso(entry.created_at),
    }


def _memory_rows(db: Session, workspace_id: str) -> Iterator[Memory]:
    statement = (
        select(Memory)
        .where(Memory.workspace_id == workspace_id)
        .order_by(Memory.created_at.asc(), Memory.id.asc())
        .execution_options(yield_per=200)
    )
    yield from db.scalars(statement)


def _episode_rows(db: Session, workspace_id: str) -> Iterator[Episode]:
    statement = (
        select(Episode)
        .join(SessionModel, Episode.session_id == SessionModel.id)
        .where(SessionModel.workspace_id == workspace_id)
        .order_by(Episode.created_at.asc(), Episode.id.asc())
        .execution_options(yield_per=200)
    )
    yield from db.scalars(statement)


def _audit_rows(db: Session, workspace_id: str) -> Iterator[AuditLog]:
    statement = (
        select(AuditLog)
        .where(AuditLog.workspace_id == workspace_id)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        .execution_options(yield_per=200)
    )
    yield from db.scalars(statement)


def _write_json_array(
    handle: Any,
    records: Iterable[dict[str, Any]],
) -> int:
    handle.write("[")
    count = 0
    for record in records:
        if count:
            handle.write(",")
        json.dump(record, handle, ensure_ascii=False, default=str)
        count += 1
    handle.write("]")
    return count


def _write_json(
    handle: Any,
    db: Session,
    workspace: Workspace,
) -> None:
    handle.write("{")
    handle.write('"workspace":')
    json.dump(
        _workspace_record(workspace), handle, ensure_ascii=False, default=str
    )
    handle.write(',"exported_at":')
    json.dump(datetime.now().astimezone().isoformat(), handle)
    handle.write(',"memories":')
    _write_json_array(
        handle, (_memory_record(row) for row in _memory_rows(db, workspace.id))
    )
    handle.write(',"episodes":')
    _write_json_array(
        handle, (_episode_record(row) for row in _episode_rows(db, workspace.id))
    )
    handle.write(',"audit":')
    _write_json_array(
        handle, (_audit_record(row) for row in _audit_rows(db, workspace.id))
    )
    handle.write("}")


def _write_markdown(
    handle: Any,
    db: Session,
    workspace: Workspace,
) -> None:
    handle.write(f"# 工作区导出：{workspace.name}\n\n")
    handle.write(f"- 工作区 ID：`{workspace.id}`\n")
    handle.write(f"- 导出时间：{datetime.now().astimezone().isoformat()}\n\n")

    handle.write("## 记忆\n\n")
    current_type: str | None = None
    memory_count = 0
    for memory in _memory_rows(db, workspace.id):
        if memory.type != current_type:
            current_type = memory.type
            handle.write(f"### {current_type}\n\n")
        memory_count += 1
        handle.write(f"- **{memory.id}** ({memory.status}, {memory.scope})\n")
        handle.write(f"  - 内容：{memory.content}\n")
        if memory.summary:
            handle.write(f"  - 摘要：{memory.summary}\n")
        handle.write(
            f"  - importance={memory.importance} confidence={memory.confidence} "
            f"access_count={memory.access_count} version={memory.version}\n"
        )
        if memory.valid_to:
            handle.write(f"  - valid_to：{_iso(memory.valid_to)}\n")
        handle.write("\n")
    if memory_count == 0:
        handle.write("（无）\n\n")

    handle.write("## 情景\n\n")
    episode_count = 0
    for episode in _episode_rows(db, workspace.id):
        episode_count += 1
        handle.write(f"### {episode.title or episode.id}\n\n")
        handle.write(f"- ID：`{episode.id}`\n")
        handle.write(f"- 会话：`{episode.session_id}`\n")
        if episode.summary:
            handle.write(f"- 摘要：{episode.summary}\n")
        if episode.outcome:
            handle.write(f"- 结果：{episode.outcome}\n")
        handle.write("\n")
    if episode_count == 0:
        handle.write("（无）\n\n")

    handle.write("## 审计\n\n")
    audit_count = 0
    for entry in _audit_rows(db, workspace.id):
        audit_count += 1
        handle.write(
            f"- {_iso(entry.created_at)} `{entry.action}` "
            f"{entry.target_type}/{entry.target_id} "
            f"({entry.actor_type})\n"
        )
    if audit_count == 0:
        handle.write("（无）\n")


def export_workspace(
    db: Session,
    workspace_id: str,
    fmt: ExportFormat = "json",
) -> Path:
    """把一个工作区流式导出到临时文件，返回文件路径。

    ``fmt`` 只接受 ``json`` 与 ``md``；工作区不存在时抛 ``ValueError``。
    调用方负责审计与 job_run 状态更新。
    """
    normalized = str(fmt).lower()
    if normalized not in EXPORT_FORMATS:
        raise ValueError(f"不支持的导出格式: {fmt!r}")
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError(f"工作区不存在: {workspace_id}")

    export_dir = Path(get_current_settings().export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=export_dir,
        prefix=f"export-{workspace_id}-",
        suffix=f".{normalized}",
    )
    path = Path(handle.name)
    try:
        with handle:
            if normalized == "json":
                _write_json(handle, db, workspace)
            else:
                _write_markdown(handle, db, workspace)
    except Exception:
        path.unlink(missing_ok=True)
        raise

    logger.info(
        "workspace exported",
        extra={
            "workspace_id": workspace_id,
            "format": normalized,
            "path": str(path),
            "bytes": path.stat().st_size,
        },
    )
    return path


def create_export_job(
    db: Session,
    workspace_id: str,
    fmt: ExportFormat = "json",
    *,
    actor_type: str = "admin",
    actor_id: str | None = None,
) -> JobRun:
    """创建 pending 导出任务；路由提交后由 worker 执行。"""
    normalized = str(fmt).lower()
    if normalized not in EXPORT_FORMATS:
        raise ValueError(f"不支持的导出格式: {fmt!r}")
    job = JobRun(
        type="export",
        scope=workspace_id,
        status="pending",
        params={
            "workspace_id": workspace_id,
            "format": normalized,
            "actor_type": actor_type,
            "actor_id": actor_id,
        },
    )
    db.add(job)
    db.flush()
    return job


def publish_job(job_id: str, event: dict[str, Any]) -> None:
    """向 SSE 频道发布进度；Redis 不可用不影响导出。"""
    try:
        import redis

        client = redis.Redis.from_url(get_current_settings().redis_url)
        try:
            client.publish(
                f"jobs:{job_id}:events",
                json.dumps(event, ensure_ascii=False, default=str),
            )
        finally:
            client.close()
    except Exception:
        logger.warning("export event publish failed", extra={"job_id": job_id})


__all__ = [
    "EXPORT_FORMATS",
    "ExportFormat",
    "create_export_job",
    "export_workspace",
    "publish_job",
]
