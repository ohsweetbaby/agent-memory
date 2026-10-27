"""导出文件下载（规格 §11）。

导出本身异步执行；这里只把已完成任务的临时文件回给同 workspace 的读取者。
任务元数据（含文件路径）通过 ``job_run.params.workspace_id`` 隔离，跨租户
一律 404。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth.session import AuthContext, require_scope
from app.config import get_current_settings
from app.db import get_db
from app.errors import NotFoundError
from app.models import JobRun
from app.schemas.memory import ensure_no_nul

router = APIRouter(prefix="/api/v1/export", tags=["export"])


@router.get("/{job_id}")
def download_export(
    job_id: str,
    context: AuthContext = Depends(require_scope("read")),
    session: Session = Depends(get_db),
) -> FileResponse:
    ensure_no_nul(job_id, "id")
    job = session.get(JobRun, job_id)
    if job is None or job.type != "export":
        raise NotFoundError("导出任务不存在")
    workspace_id = (job.params or {}).get("workspace_id")
    if context.workspace_id is not None and workspace_id != context.workspace_id:
        raise NotFoundError("导出任务不存在")
    if job.status != "completed":
        raise NotFoundError("导出尚未完成")

    raw_path = (job.result or {}).get("path")
    if not raw_path:
        raise NotFoundError("导出文件不存在")
    path = Path(str(raw_path))
    export_root = Path(get_current_settings().export_dir).resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(export_root)
    except (OSError, ValueError):
        raise NotFoundError("导出文件不存在") from None
    if not resolved.is_file():
        raise NotFoundError("导出文件不存在")

    fmt = str((job.params or {}).get("format") or "json")
    media_type = (
        "application/json" if fmt == "json" else "text/markdown; charset=utf-8"
    )
    return FileResponse(
        resolved,
        media_type=media_type,
        filename=resolved.name,
    )


__all__ = ["router"]
