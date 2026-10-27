"""Worker/运维 CLI：从 Postgres 重放并重建外部索引。

用法::

    python -m app.worker_cli rebuild --target qdrant|neo4j|all --workspace WS
    python -m app.worker_cli rebuild --target qdrant|neo4j|all --all

``--workspace`` 与 ``--all`` 必须显式二选一：前者只影响该 workspace，
后者才跨租户全量重建并打印警告。Qdrant 重建先按 id upsert 再清理孤儿
point；Neo4j 按 workspace 删除节点；``projection_state`` 同步收窄。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.jobs.runtime import Runtime, build_runtime, get_runtime
from app.jobs.tasks import (
    NEO4J_TARGET,
    project_neo4j_record,
    project_qdrant_record,
    qdrant_target,
)
from app.logging import configure_logging
from app.models import Memory, ProjectionState

logger = logging.getLogger(__name__)

_REMOVED_STATUSES = frozenset({"deleted", "purged"})
_TARGETS = ("qdrant", "neo4j", "all")
_DELETE_CHUNK = 500


def _is_removed(memory: Memory) -> bool:
    return memory.status in _REMOVED_STATUSES or memory.deleted_at is not None


def _batch(
    db: Session, last_id: str, batch_size: int, workspace: str | None
) -> list[Memory]:
    statement = (
        select(Memory)
        .where(Memory.id > last_id)
        .order_by(Memory.id)
        .limit(batch_size)
    )
    if workspace is not None:
        statement = statement.where(Memory.workspace_id == workspace)
    return list(db.scalars(statement))


def _clear_states(db: Session, target: str, workspace: str | None) -> None:
    """只清目标索引 +（可选）workspace 的投影状态。"""
    targets = [target]
    if target.startswith("qdrant:") and target != "qdrant":
        targets.append("qdrant")  # 兼容升级前不带 collection 身份的旧行
    statement = delete(ProjectionState).where(
        ProjectionState.target.in_(targets)
    )
    if workspace is not None:
        statement = statement.where(
            ProjectionState.memory_id.in_(
                select(Memory.id).where(Memory.workspace_id == workspace)
            )
        )
    db.execute(statement)
    db.commit()


def _replay_qdrant(
    db: Session,
    runtime: Runtime,
    batch_size: int,
    workspace: str | None,
) -> int:
    """先 upsert 全部记忆，再删除孤儿 point；重建期间不清空 collection。"""
    runtime.qdrant.ensure_collection()
    _clear_states(db, qdrant_target(runtime.qdrant), workspace)

    total = 0
    keep_ids: set[str] = set()
    last_id = ""
    while True:
        rows = _batch(db, last_id, batch_size, workspace)
        if not rows:
            break
        for memory in rows:
            if _is_removed(memory):
                continue
            # 重建是显式运维动作：跳过幂等检查并重算嵌入，确保不使用旧缓存。
            project_qdrant_record(db, memory, memory.version, force=True)
            keep_ids.add(memory.id)
            total += 1
        db.commit()
        last_id = rows[-1].id

    existing = runtime.qdrant.list_ids(workspace)
    stale = [point_id for point_id in existing if point_id not in keep_ids]
    for start in range(0, len(stale), _DELETE_CHUNK):
        runtime.qdrant.delete_many(stale[start : start + _DELETE_CHUNK])
    if stale:
        logger.info(
            "qdrant orphan points removed",
            extra={"workspace_id": workspace, "count": len(stale)},
        )
    return total


def _replay_neo4j(
    db: Session,
    runtime: Runtime,
    batch_size: int,
    workspace: str | None,
) -> int:
    if runtime.neo4j is None:
        logger.warning("neo4j is not configured; rebuild skipped")
        return 0
    _clear_states(db, NEO4J_TARGET, workspace)
    runtime.neo4j.clear(workspace)

    total = 0
    last_id = ""
    while True:
        rows = _batch(db, last_id, batch_size, workspace)
        if not rows:
            break
        for memory in rows:
            if _is_removed(memory):
                continue
            project_neo4j_record(db, memory, memory.version, force=True)
            total += 1
        db.commit()
        last_id = rows[-1].id
    return total


def run_rebuild(
    target: str,
    *,
    workspace: str | None = None,
    all_workspaces: bool = False,
    batch_size: int = 200,
    runtime: Runtime | None = None,
) -> dict[str, int]:
    """重建指定目标。

    必须显式二选一：``workspace`` 只重建单个 workspace；``all_workspaces``
    才执行跨租户全量重建。两者同时或缺省都拒绝执行。
    """
    normalized = target.strip().lower()
    if normalized not in _TARGETS:
        raise ValueError(f"不支持的目标: {target!r}，可选 {', '.join(_TARGETS)}")
    if all_workspaces and workspace is not None:
        raise ValueError("workspace 与 all_workspaces 不能同时指定")
    if not all_workspaces and workspace is None:
        raise ValueError("必须指定 workspace 或显式传入 all_workspaces")
    if workspace is not None:
        workspace = workspace.strip()
        if not workspace:
            raise ValueError("workspace 不能为空或空白")

    resolved = runtime or get_runtime()
    scope = None if all_workspaces else workspace
    result: dict[str, int] = {}

    if normalized in {"qdrant", "all"}:
        with resolved.session_scope() as db:
            result["qdrant"] = _replay_qdrant(db, resolved, batch_size, scope)
        logger.info(
            "qdrant rebuild finished",
            extra={"workspace_id": scope, "count": result["qdrant"]},
        )

    if normalized in {"neo4j", "all"}:
        with resolved.session_scope() as db:
            result["neo4j"] = _replay_neo4j(db, resolved, batch_size, scope)
        logger.info(
            "neo4j rebuild finished",
            extra={"workspace_id": scope, "count": result["neo4j"]},
        )

    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.worker_cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    rebuild = subparsers.add_parser("rebuild", help="从 Postgres 重放外部索引")
    rebuild.add_argument(
        "--target",
        required=True,
        choices=list(_TARGETS),
        help="要重建的索引",
    )
    scope = rebuild.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--workspace",
        default=None,
        help="只重建指定 workspace（不能为空）",
    )
    scope.add_argument(
        "--all",
        action="store_true",
        dest="all_workspaces",
        help="跨租户全量重建；会重写所有 workspace 的索引",
    )
    rebuild.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="每批读取的记忆数量",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "rebuild":
        workspace = args.workspace
        if workspace is not None:
            workspace = workspace.strip()
            if not workspace:
                parser.error("--workspace 不能为空或空白")
        configure_logging(get_settings().log_level)
        if args.all_workspaces:
            print(
                "WARNING: 正在跨租户全量重建索引，"
                "将重写所有 workspace 的 Qdrant/Neo4j 投影。",
                file=sys.stderr,
            )

        runtime = build_runtime()
        try:
            result = run_rebuild(
                args.target,
                workspace=workspace,
                all_workspaces=args.all_workspaces,
                batch_size=args.batch_size,
                runtime=runtime,
            )
        finally:
            runtime.close()
        print(
            json.dumps(
                {
                    "target": args.target,
                    "workspace": workspace,
                    "all_workspaces": args.all_workspaces,
                    **result,
                },
                ensure_ascii=False,
            )
        )
        return 0

    return 1  # pragma: no cover - argparse 已限制取值


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main", "run_rebuild"]
