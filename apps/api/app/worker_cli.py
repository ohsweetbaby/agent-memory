"""Worker/运维 CLI：从 Postgres 重放并重建外部索引。

用法::

    python -m app.worker_cli rebuild --target qdrant|neo4j|all

重建总是先清空目标索引（Qdrant collection / Neo4j M1 节点），再按 id 分页
读取 ``memory`` 重放；Postgres 是唯一事实源，嵌入缺失时会现场补齐。
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.jobs.runtime import Runtime, build_runtime, get_runtime
from app.jobs.tasks import project_neo4j_record, project_qdrant_record
from app.logging import configure_logging
from app.models import Memory, ProjectionState

logger = logging.getLogger(__name__)

_REMOVED_STATUSES = frozenset({"deleted", "purged"})
_TARGETS = ("qdrant", "neo4j", "all")


def _is_removed(memory: Memory) -> bool:
    return memory.status in _REMOVED_STATUSES or memory.deleted_at is not None


def _batch(db: Session, last_id: str, batch_size: int) -> list[Memory]:
    statement = (
        select(Memory)
        .where(Memory.id > last_id)
        .order_by(Memory.id)
        .limit(batch_size)
    )
    return list(db.scalars(statement))


def _replay_qdrant(db: Session, runtime: Runtime, batch_size: int) -> int:
    total = 0
    last_id = ""
    while True:
        rows = _batch(db, last_id, batch_size)
        if not rows:
            break
        for memory in rows:
            if _is_removed(memory):
                continue
            project_qdrant_record(db, memory, memory.version, force=True)
            total += 1
        db.commit()
        last_id = rows[-1].id
    return total


def _replay_neo4j(db: Session, runtime: Runtime, batch_size: int) -> int:
    if runtime.neo4j is None:
        logger.warning("neo4j is not configured; rebuild skipped")
        return 0
    total = 0
    last_id = ""
    while True:
        rows = _batch(db, last_id, batch_size)
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


def _clear_states(db: Session, target: str) -> None:
    db.execute(delete(ProjectionState).where(ProjectionState.target == target))
    db.commit()


def run_rebuild(
    target: str,
    *,
    batch_size: int = 200,
    runtime: Runtime | None = None,
) -> dict[str, int]:
    """重建指定目标；返回每个目标实际重放的记忆数量。"""
    normalized = target.strip().lower()
    if normalized not in _TARGETS:
        raise ValueError(f"不支持的目标: {target!r}，可选 {', '.join(_TARGETS)}")
    resolved = runtime or get_runtime()
    result: dict[str, int] = {}

    if normalized in {"qdrant", "all"}:
        resolved.qdrant.recreate_collection()
        with resolved.session_scope() as db:
            _clear_states(db, "qdrant")
            result["qdrant"] = _replay_qdrant(db, resolved, batch_size)
        logger.info("qdrant rebuild finished", extra={"count": result["qdrant"]})

    if normalized in {"neo4j", "all"}:
        if resolved.neo4j is not None:
            resolved.neo4j.clear()
        with resolved.session_scope() as db:
            _clear_states(db, "neo4j")
            result["neo4j"] = _replay_neo4j(db, resolved, batch_size)
        logger.info("neo4j rebuild finished", extra={"count": result["neo4j"]})

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
    rebuild.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="每批读取的记忆数量",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    configure_logging(get_settings().log_level)

    if args.command == "rebuild":
        runtime = build_runtime()
        try:
            result = run_rebuild(
                args.target,
                batch_size=args.batch_size,
                runtime=runtime,
            )
        finally:
            runtime.close()
        print(json.dumps({"target": args.target, **result}, ensure_ascii=False))
        return 0

    return 1  # pragma: no cover - argparse 已限制取值


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main", "run_rebuild"]
