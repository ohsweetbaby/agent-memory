"""检索性能基线：1 万条记忆、100 次查询，P95 < 300ms。

用真实 Postgres + Qdrant + Neo4j；数据由 ``seeded_10k`` 批量写入，
不经过 1 万次 API/写入管线调用。默认 marker ``perf`` 不参与普通测试。
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.perf


def test_search_p95_under_300ms(seeded_10k, measure_search) -> None:
    assert seeded_10k.memory_count >= 10_000

    iterations = int(os.getenv("PERF_ITERS", "100"))
    report = measure_search(iterations=iterations)

    assert report.iterations == iterations
    assert report.p95_ms < 300, report.summary()
    print(report.summary())
