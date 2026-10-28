"""性能基准的共享数据与测量工具。

默认 marker ``perf`` 不参与普通测试；需要真实容器与 1 万条数据时显式运行：

    uv run pytest apps/api/tests/perf -m perf -v -s
"""

from __future__ import annotations

import asyncio
import cProfile
import math
import os
import pstats
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import insert

from app.adapters.embeddings import FakeEmbedding
from app.adapters.qdrant import QdrantIndex
from app.db import build_session_factory
from app.models import Memory, Workspace
from app.models.base import utcnow
from app.schemas.search import SearchRequest
from app.services.search import search_memories

PERF_WORKSPACE = "ws_perf"
PERF_MEMORIES = 10_000
PERF_DIM = 64
PERF_MODEL = f"fake-{PERF_DIM}"


@dataclass(frozen=True, slots=True)
class PerfReport:
    iterations: int
    p50_ms: float
    p95_ms: float
    max_ms: float

    def summary(self) -> str:
        return (
            f"iterations={self.iterations} p50={self.p50_ms:.2f}ms "
            f"p95={self.p95_ms:.2f}ms max={self.max_ms:.2f}ms"
        )


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[rank]


@pytest.fixture(scope="module")
def seeded_10k(stack):
    """一个独立库 + 1 万条事实源 + 对应的 Qdrant 投影。"""
    engine = stack.new_engine("perf")
    stack.migrate(engine)
    factory = build_session_factory(engine)

    qdrant = QdrantIndex(
        stack.qdrant_url, provider="fake", model=PERF_MODEL, dim=PERF_DIM
    )
    qdrant.recreate_collection()
    embedding = FakeEmbedding(dim=PERF_DIM)

    now = utcnow()
    rows: list[dict[str, object]] = []
    contents: list[str] = []
    memory_types = ("semantic", "episodic", "procedural")
    topics = (
        "数据迁移与回滚演练",
        "咖啡豆产区与烘焙曲线",
        "Kubernetes 节点扩缩容",
        "季度预算与成本分摊",
        "用户访谈记录整理",
        "向量检索召回评估",
        "图数据库慢查询排查",
        "移动端离线缓存策略",
        "合同条款风险复核",
        "周末徒步路线规划",
        "缓存击穿与降级方案",
        "团队值班轮换安排",
        "产品路线图评审",
        "日志采集与脱敏规则",
        "数据库索引重建",
        "多语言文案校对",
        "支付对账差异分析",
        "客户投诉归因",
        "实验分组与显著性",
        "供应链库存预警",
        "代码评审检查清单",
        "接口限流与熔断",
        "知识库切片策略",
        "演示环境数据准备",
        "安全基线扫描",
        "文档版本归档",
        "招聘面试反馈汇总",
        "模型成本核算",
        "监控告警降噪",
        "异地容灾演练",
    )
    for index in range(PERF_MEMORIES):
        memory_id = str(uuid4())
        first = topics[index % len(topics)]
        second = topics[(index * 7 + 3) % len(topics)]
        content = f"记录 {index:05d}：{first}；{second}。"
        contents.append(content)
        rows.append(
            {
                "id": memory_id,
                "workspace_id": PERF_WORKSPACE,
                "scope": "global",
                "scope_id": None,
                "type": memory_types[index % len(memory_types)],
                "content": content,
                "summary": None,
                "idempotency_key": f"perf:{index}",
                "importance": 0.4 + (index % 60) / 100.0,
                "confidence": 0.7,
                "stability": 1.0,
                "access_count": index % 5,
                "version": 1,
                "last_reinforced_at": now,
                "last_accessed_at": None,
                "valid_from": now,
                "valid_to": None,
                "superseded_by": None,
                "status": "active",
                "source_type": "manual",
                "source_id": f"perf:{index}",
                "metadata": {"bench": True},
                "consolidation_run_id": None,
                "deleted_at": None,
            }
        )

    with factory() as session:
        session.add(Workspace(id=PERF_WORKSPACE, name="性能基准"))
        session.flush()
        for start in range(0, len(rows), 500):
            session.execute(insert(Memory.__table__), rows[start : start + 500])
        session.commit()

    vectors = embedding.embed_sync(contents)
    points: list[tuple[str, list[float], dict[str, object]]] = []
    for row, vector in zip(rows, vectors, strict=True):
        points.append(
            (
                str(row["id"]),
                vector,
                {
                    "workspace_id": PERF_WORKSPACE,
                    "scope": "global",
                    "scope_id": None,
                    "type": row["type"],
                    "status": "active",
                    "importance": row["importance"],
                    "valid_to": None,
                    "last_reinforced_at": now,
                    "entity_ids": [],
                    "version": 1,
                },
            )
        )
    qdrant.upsert_many(points)

    seeded = SimpleNamespace(
        engine=engine,
        factory=factory,
        qdrant=qdrant,
        embedding=embedding,
        memory_count=len(rows),
    )
    try:
        yield seeded
    finally:
        try:
            qdrant.delete_collection()
        finally:
            qdrant.close()
        engine.dispose()


@pytest.fixture(scope="module")
def measure_search(seeded_10k):
    """执行 N 次真实检索，返回延迟分位数。"""

    def _once(index: int) -> float:
        with seeded_10k.factory() as session:
            request = SearchRequest(
                query=f"{index:05d}",
                workspace_id=PERF_WORKSPACE,
                limit=10,
                mode="balanced",
            )
            result = asyncio.run(
                search_memories(
                    session,
                    request,
                    seeded_10k.embedding,
                    seeded_10k.qdrant,
                    None,
                )
            )
            return float(result.stats.latency_ms)

    def _run(iterations: int = 100) -> PerfReport:
        latencies: list[float] = []
        for index in range(iterations):
            if index == 0 and os.getenv("PERF_PROFILE") == "1":
                profiler = cProfile.Profile()
                profiler.enable()
                latency = _once(index)
                profiler.disable()
                pstats.Stats(profiler).sort_stats("cumulative").print_stats(30)
                latencies.append(latency)
            else:
                latencies.append(_once(index))
        return PerfReport(
            iterations=iterations,
            p50_ms=_nearest_rank(latencies, 0.50),
            p95_ms=_nearest_rank(latencies, 0.95),
            max_ms=max(latencies),
        )

    return _run


__all__ = ["PerfReport", "measure_search", "seeded_10k"]
