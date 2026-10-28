"""黄金集评测脚本。

用法::

    uv run python apps/api/tests/eval/run_eval.py --provider fake
    uv run python apps/api/tests/eval/run_eval.py --provider real

``fake`` 使用确定性词面哈希嵌入与 testcontainers（Postgres/Qdrant/Neo4j），
CI 可在没有真实模型密钥时稳定复现；``real`` 读取 ``DATABASE_URL``、
``QDRANT_URL``、``NEO4J_*`` 与嵌入 Provider 环境变量，对已迁移的实例执行
同一套查询，不自行启动容器。

报告写入 ``artifacts/eval-{timestamp}.json``（仓库根目录）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete, insert, inspect, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.embeddings import FakeEmbedding, build_embedding_provider
from app.adapters.neo4j import GraphEntity, Neo4jIndex
from app.adapters.qdrant import QdrantIndex
from app.config import Settings
from app.db import build_engine, build_session_factory
from app.models import Entity, Memory, MemoryEntity, Workspace
from app.models.base import utcnow
from app.schemas.search import SearchRequest
from app.services.search import search_memories
from memory_core.context import estimate_tokens

EVAL_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = EVAL_DIR.parent / "fixtures" / "golden"
REPO_ROOT = EVAL_DIR.parents[3]
DEFAULT_ARTIFACT_DIR = REPO_ROOT / "artifacts"

GOLDEN_WORKSPACE = "ws_golden"
GOLDEN_NAMESPACE = uuid5(NAMESPACE_URL, "agent-memory-golden-v1")
FAKE_DIM = 64
FAKE_MODEL = f"fake-{FAKE_DIM}"

CATEGORIES = (
    "single_hop",
    "multi_hop",
    "temporal",
    "contradiction",
    "negation",
    "scope",
    "procedural",
)

_TESTS_DIR = EVAL_DIR.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _fixture_path(name: str) -> Path:
    return FIXTURES_DIR / name


def load_memories(path: Path | None = None) -> list[dict[str, Any]]:
    payload = json.loads((path or _fixture_path("memories.json")).read_text("utf-8"))
    memories = payload["memories"] if isinstance(payload, dict) else payload
    if not isinstance(memories, list):
        raise ValueError("memories.json 必须是数组或 {memories: [...]}")
    return memories


def load_queries(path: Path | None = None) -> list[dict[str, Any]]:
    payload = json.loads((path or _fixture_path("queries.json")).read_text("utf-8"))
    queries = payload["queries"] if isinstance(payload, dict) else payload
    if not isinstance(queries, list):
        raise ValueError("queries.json 必须是数组或 {queries: [...]}")
    return queries


def golden_memory_id(key: str) -> str:
    return str(uuid5(GOLDEN_NAMESPACE, f"memory:{key}"))


def golden_entity_id(name: str) -> str:
    return str(uuid5(GOLDEN_NAMESPACE, f"entity:{name}"))


# ---------------------------------------------------------------------------
# 评测环境
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EvaluationEnv:
    """一次评测使用的真实依赖；由 pytest fixture 或 CLI 构造。"""

    engine: Engine
    embedding: Any
    qdrant: Any
    neo4j: Any


@dataclass(slots=True)
class CategoryReport:
    query_count: int = 0
    recall_at_5: float = 0.0
    mrr: float = 0.0


@dataclass(slots=True)
class EvalReport:
    provider: str
    workspace: str
    memory_count: int
    query_count: int
    recall_at_5: float
    mrr: float
    stale_fact_rate: float
    contradiction_accuracy: float
    scope_leak_rate: float
    avg_tokens: float
    p95_ms: float
    p50_ms: float
    avg_ms: float
    embedding: str
    qdrant_collection: str
    generated_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    by_category: dict[str, CategoryReport] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    report_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[rank]


def _materialize(
    session: Session, memories: Sequence[dict[str, Any]]
) -> dict[str, Memory]:
    """清空黄金集 workspace 后批量写事实源（直接落库，不走写入 API）。"""
    session.execute(delete(Memory).where(Memory.workspace_id == GOLDEN_WORKSPACE))
    session.execute(delete(Entity).where(Entity.workspace_id == GOLDEN_WORKSPACE))
    session.execute(delete(Workspace).where(Workspace.id == GOLDEN_WORKSPACE))
    session.add(Workspace(id=GOLDEN_WORKSPACE, name="黄金集评测"))
    session.flush()

    now = utcnow()
    ids = {memory["key"]: golden_memory_id(memory["key"]) for memory in memories}
    rows: list[dict[str, Any]] = []
    superseded_pairs: list[tuple[str, str]] = []
    for memory in memories:
        key = memory["key"]
        age_days = int(memory.get("age_days") or 0)
        reinforced = now - timedelta(days=age_days)
        valid_to = None
        if memory.get("valid_to_age_days") is not None:
            valid_to = now - timedelta(days=int(memory["valid_to_age_days"]))
        if memory.get("superseded_by_key"):
            superseded_pairs.append((key, memory["superseded_by_key"]))
        metadata = dict(memory.get("metadata") or {})
        metadata["golden_key"] = key
        rows.append(
            {
                "id": ids[key],
                "workspace_id": GOLDEN_WORKSPACE,
                "scope": memory.get("scope") or "user",
                "scope_id": memory.get("scope_id"),
                "type": memory.get("type") or "semantic",
                "content": memory["content"],
                "summary": None,
                "idempotency_key": f"golden:{key}",
                "importance": float(memory.get("importance", 0.8)),
                "confidence": float(memory.get("confidence", 0.9)),
                "stability": 1.0,
                "access_count": 0,
                "version": 1,
                "last_reinforced_at": reinforced,
                "last_accessed_at": None,
                "valid_from": reinforced,
                "valid_to": valid_to,
                # 先留空，全部行插入后再回填，避免自引用外键在批量 INSERT
                # 中指向尚未写入的替代目标。
                "superseded_by": None,
                "status": memory.get("status") or "active",
                "source_type": "message",
                "source_id": f"golden:{key}",
                "metadata": metadata,
                "consolidation_run_id": None,
                "deleted_at": None,
            }
        )

    for start in range(0, len(rows), 500):
        session.execute(insert(Memory.__table__), rows[start : start + 500])
    for source_key, target_key in superseded_pairs:
        session.execute(
            update(Memory)
            .where(Memory.id == ids[source_key])
            .values(superseded_by=ids[target_key])
        )

    entity_names = sorted(
        {name for memory in memories for name in memory.get("entities") or []}
    )
    if entity_names:
        session.execute(
            insert(Entity.__table__),
            [
                {
                    "id": golden_entity_id(name),
                    "workspace_id": GOLDEN_WORKSPACE,
                    "name": name,
                    "type": None,
                    "canonical_name": None,
                }
                for name in entity_names
            ],
        )
        links: list[dict[str, Any]] = []
        for memory in memories:
            memory_id = ids[memory["key"]]
            for name in memory.get("entities") or []:
                entity_id = golden_entity_id(name)
                links.append(
                    {
                        "id": str(
                            uuid5(
                                GOLDEN_NAMESPACE,
                                f"memory_entity:{memory_id}:{entity_id}",
                            )
                        ),
                        "memory_id": memory_id,
                        "entity_id": entity_id,
                        "role": None,
                    }
                )
        for start in range(0, len(links), 1000):
            session.execute(
                insert(MemoryEntity.__table__), links[start : start + 1000]
            )

    session.commit()

    loaded = session.scalars(
        select(Memory).where(Memory.workspace_id == GOLDEN_WORKSPACE)
    ).all()
    by_id = {memory.id: memory for memory in loaded}
    return {key: by_id[memory_id] for key, memory_id in ids.items()}


def _index_qdrant(
    qdrant: Any,
    embedding: Any,
    memories: Sequence[Memory],
    entity_ids: dict[str, list[str]],
) -> int:
    qdrant.recreate_collection()
    vectors = embedding.embed_sync([memory.content for memory in memories])
    points = []
    for memory, vector in zip(memories, vectors):
        points.append(
            (
                memory.id,
                vector,
                {
                    "workspace_id": memory.workspace_id,
                    "scope": memory.scope,
                    "scope_id": memory.scope_id,
                    "type": memory.type,
                    "status": memory.status,
                    "importance": float(memory.importance or 0.0),
                    "valid_to": memory.valid_to,
                    "last_reinforced_at": memory.last_reinforced_at,
                    "entity_ids": entity_ids.get(memory.id, []),
                    "version": memory.version,
                },
            )
        )
    return qdrant.upsert_many(points)


def _index_neo4j(
    neo4j: Any,
    memories: Sequence[Memory],
    entity_names: dict[str, list[str]],
) -> int:
    neo4j.clear(GOLDEN_WORKSPACE)
    records = []
    for memory in memories:
        entities = [
            GraphEntity(id=golden_entity_id(name), name=name)
            for name in entity_names.get(memory.id, [])
        ]
        records.append((memory, entities, ()))
    return neo4j.upsert_many(records)


def _entity_ids_by_memory(
    memories: Sequence[dict[str, Any]],
) -> dict[str, list[str]]:
    return {
        golden_memory_id(memory["key"]): [
            golden_entity_id(name) for name in memory.get("entities") or []
        ]
        for memory in memories
    }


def _entity_names_by_id(
    memories: Sequence[dict[str, Any]],
) -> dict[str, list[str]]:
    return {
        golden_memory_id(memory["key"]): list(memory.get("entities") or [])
        for memory in memories
    }


async def _evaluate(
    session_factory: sessionmaker[Session],
    env: EvaluationEnv,
    queries: Sequence[dict[str, Any]],
    key_by_id: dict[str, str],
) -> tuple[
    list[float],
    list[float],
    list[float],
    list[float],
    list[bool],
    list[bool],
    list[bool],
    list[dict[str, Any]],
    dict[str, list[tuple[float, float]]],
]:
    """执行全部查询，返回逐条指标，便于脚本与 pytest 共用。"""
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    token_counts: list[float] = []
    latencies: list[float] = []
    stale_hits: list[bool] = []
    contradictions: list[bool] = []
    leaks: list[bool] = []
    failures: list[dict[str, Any]] = []
    category_values: dict[str, list[tuple[float, float]]] = defaultdict(list)

    with session_factory() as session:
        for item in queries:
            expected = [golden_memory_id(key) for key in item.get("expected_keys", [])]
            stale = [golden_memory_id(key) for key in item.get("stale_keys", [])]
            forbidden = [
                golden_memory_id(key) for key in item.get("forbidden_keys", [])
            ]
            request = SearchRequest(
                query=item["query"],
                workspace_id=GOLDEN_WORKSPACE,
                agent_id=item.get("agent_id"),
                end_user_id=item.get("end_user_id"),
                session_id=item.get("session_id"),
                scopes=item.get("scopes"),
                types=item.get("types"),
                mode=item.get("mode") or "balanced",
                limit=int(item.get("limit") or 10),
            )
            result = await search_memories(
                session,
                request,
                env.embedding,
                env.qdrant,
                env.neo4j,
            )
            returned = [entry.id for entry in result.items]
            top5 = returned[:5]
            hits = [memory_id for memory_id in expected if memory_id in top5]
            recall = len(hits) / len(expected) if expected else 0.0
            reciprocal = 0.0
            for rank, memory_id in enumerate(returned, start=1):
                if memory_id in expected:
                    reciprocal = 1.0 / rank
                    break
            stale_hit = any(memory_id in top5 for memory_id in stale)
            leak = any(memory_id in returned for memory_id in forbidden)
            contradiction_ok = (
                item["category"] != "contradiction"
                or (bool(hits) and not stale_hit)
            )
            tokens = estimate_tokens(result.context_text)
            latency = float(result.stats.latency_ms)

            recalls.append(recall)
            reciprocal_ranks.append(reciprocal)
            token_counts.append(float(tokens))
            latencies.append(latency)
            stale_hits.append(stale_hit)
            contradictions.append(contradiction_ok)
            leaks.append(leak)
            category_values[item["category"]].append((recall, reciprocal))

            if recall < 1.0 or stale_hit or leak:
                failures.append(
                    {
                        "id": item["id"],
                        "category": item["category"],
                        "query": item["query"],
                        "expected": item.get("expected_keys", []),
                        "stale": item.get("stale_keys", []),
                        "forbidden": item.get("forbidden_keys", []),
                        "top5": [key_by_id.get(memory_id, memory_id) for memory_id in top5],
                        "degraded": result.degraded,
                    }
                )

    return (
        recalls,
        reciprocal_ranks,
        token_counts,
        latencies,
        stale_hits,
        contradictions,
        leaks,
        failures,
        category_values,
    )


def _build_report(
    *,
    provider: str,
    memories: Sequence[dict[str, Any]],
    queries: Sequence[dict[str, Any]],
    env: EvaluationEnv,
    recalls: list[float],
    reciprocal_ranks: list[float],
    token_counts: list[float],
    latencies: list[float],
    stale_hits: list[bool],
    contradictions: list[bool],
    leaks: list[bool],
    failures: list[dict[str, Any]],
    category_values: dict[str, list[tuple[float, float]]],
) -> EvalReport:
    stale_queries = [
        index
        for index, item in enumerate(queries)
        if item.get("stale_keys")
    ]
    leak_queries = [
        index
        for index, item in enumerate(queries)
        if item.get("forbidden_keys")
    ]
    contradiction_queries = [
        index
        for index, item in enumerate(queries)
        if item.get("category") == "contradiction"
    ]
    by_category: dict[str, CategoryReport] = {}
    for category in CATEGORIES:
        values = category_values.get(category, [])
        if not values:
            continue
        by_category[category] = CategoryReport(
            query_count=len(values),
            recall_at_5=round(sum(value[0] for value in values) / len(values), 4),
            mrr=round(sum(value[1] for value in values) / len(values), 4),
        )

    return EvalReport(
        provider=provider,
        workspace=GOLDEN_WORKSPACE,
        memory_count=len(memories),
        query_count=len(queries),
        recall_at_5=round(statistics.fmean(recalls) if recalls else 0.0, 4),
        mrr=round(
            statistics.fmean(reciprocal_ranks) if reciprocal_ranks else 0.0, 4
        ),
        stale_fact_rate=round(
            sum(stale_hits[index] for index in stale_queries)
            / len(stale_queries),
            4,
        )
        if stale_queries
        else 0.0,
        contradiction_accuracy=round(
            sum(contradictions[index] for index in contradiction_queries)
            / len(contradiction_queries),
            4,
        )
        if contradiction_queries
        else 0.0,
        scope_leak_rate=round(
            sum(leaks[index] for index in leak_queries) / len(leak_queries), 4
        )
        if leak_queries
        else 0.0,
        avg_tokens=round(statistics.fmean(token_counts) if token_counts else 0.0, 2),
        p95_ms=round(_nearest_rank(latencies, 0.95), 2),
        p50_ms=round(_nearest_rank(latencies, 0.50), 2),
        avg_ms=round(statistics.fmean(latencies) if latencies else 0.0, 2),
        embedding=env.embedding.identity,
        qdrant_collection=str(getattr(env.qdrant, "collection", "")),
        by_category=by_category,
        failures=failures[:40],
    )


def _prepare(
    env: EvaluationEnv,
    memories: Sequence[dict[str, Any]],
) -> tuple[dict[str, Memory], dict[str, str]]:
    factory = build_session_factory(env.engine)
    with factory() as session:
        by_key = _materialize(session, memories)
        rows = list(by_key.values())
        qdrant_count = _index_qdrant(
            env.qdrant,
            env.embedding,
            rows,
            _entity_ids_by_memory(memories),
        )
        neo4j_count = _index_neo4j(
            env.neo4j, rows, _entity_names_by_id(memories)
        )
        if qdrant_count != len(rows):
            raise RuntimeError(
                f"Qdrant 索引数量不匹配: {qdrant_count} != {len(rows)}"
            )
        if neo4j_count != len(rows):
            raise RuntimeError(
                f"Neo4j 索引数量不匹配: {neo4j_count} != {len(rows)}"
            )
    key_by_id = {memory.id: key for key, memory in by_key.items()}
    return by_key, key_by_id


def run_eval(
    provider: str = "fake",
    *,
    env: EvaluationEnv | None = None,
    write_report: bool = True,
    artifact_dir: Path | None = None,
) -> EvalReport:
    """执行黄金集评测并返回报告；``env`` 允许测试注入共享容器栈。"""
    normalized = provider.strip().lower()
    if normalized not in {"fake", "real"}:
        raise ValueError("provider 必须是 fake 或 real")

    memories = load_memories()
    queries = load_queries()
    owned_stack = None

    if env is None:
        if normalized == "fake":
            from support.stack import start_stack

            owned_stack = start_stack()
            engine = owned_stack.new_engine("eval")
            owned_stack.migrate(engine)
            env = EvaluationEnv(
                engine=engine,
                embedding=FakeEmbedding(dim=FAKE_DIM),
                qdrant=QdrantIndex(
                    owned_stack.qdrant_url,
                    provider="fake",
                    model=FAKE_MODEL,
                    dim=FAKE_DIM,
                ),
                neo4j=Neo4jIndex(
                    owned_stack.neo4j_uri,
                    "neo4j",
                    "test-password",
                ),
            )
            env.neo4j.wait_ready(timeout=180.0)
        else:
            settings = Settings()
            if settings.embedding_provider.strip().lower() in {
                "fake",
                "stub",
                "test",
                "deterministic",
            }:
                raise RuntimeError(
                    "real 模式需要真实的 EMBEDDING_PROVIDER；"
                    "当前配置仍为 fake。"
                )
            engine = build_engine(settings)
            if not inspect(engine).has_table("memory"):
                raise RuntimeError(
                    "目标数据库尚未迁移；请先执行 "
                    "`uv run alembic -c apps/api/alembic.ini upgrade head`"
                )
            env = EvaluationEnv(
                engine=engine,
                embedding=build_embedding_provider(settings),
                qdrant=QdrantIndex.from_settings(settings),
                neo4j=Neo4jIndex.from_settings(settings),
            )
            env.neo4j.wait_ready(timeout=180.0)

    try:
        _, key_by_id = _prepare(env, memories)
        session_factory = build_session_factory(env.engine)
        (
            recalls,
            reciprocal_ranks,
            token_counts,
            latencies,
            stale_hits,
            contradictions,
            leaks,
            failures,
            category_values,
        ) = asyncio.run(
            _evaluate(session_factory, env, queries, key_by_id)
        )
        report = _build_report(
            provider=normalized,
            memories=memories,
            queries=queries,
            env=env,
            recalls=recalls,
            reciprocal_ranks=reciprocal_ranks,
            token_counts=token_counts,
            latencies=latencies,
            stale_hits=stale_hits,
            contradictions=contradictions,
            leaks=leaks,
            failures=failures,
            category_values=category_values,
        )
        if write_report:
            target_dir = artifact_dir or DEFAULT_ARTIFACT_DIR
            target_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = target_dir / f"eval-{timestamp}.json"
            path.write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            report.report_path = str(path)
        return report
    finally:
        if owned_stack is not None:
            owned_stack.close()


def _print_summary(report: EvalReport) -> None:
    summary = {
        "provider": report.provider,
        "memories": report.memory_count,
        "queries": report.query_count,
        "recall@5": report.recall_at_5,
        "mrr": report.mrr,
        "stale_fact_rate": report.stale_fact_rate,
        "contradiction_accuracy": report.contradiction_accuracy,
        "scope_leak_rate": report.scope_leak_rate,
        "avg_tokens": report.avg_tokens,
        "p95_ms": report.p95_ms,
        "by_category": {
            category: asdict(value)
            for category, value in report.by_category.items()
        },
        "failures": len(report.failures),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_eval.py")
    parser.add_argument("--provider", choices=["fake", "real"], default="fake")
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="不写 artifacts/eval-*.json",
    )
    parser.add_argument(
        "--fail-under-recall",
        type=float,
        default=None,
        help="低于该 recall@5 时以非零状态退出",
    )
    args = parser.parse_args(argv)

    started = time.perf_counter()
    report = run_eval(args.provider, write_report=not args.no_report)
    _print_summary(report)
    print(f"elapsed={time.perf_counter() - started:.2f}s")
    if (
        args.fail_under_recall is not None
        and report.recall_at_5 < args.fail_under_recall
    ):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
