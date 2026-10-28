"""黄金集评测：覆盖面、数据质量与 M1 基线。

评测脚本独立于 pytest 运行（``python apps/api/tests/eval/run_eval.py``），
这里用 importlib 复用同一份实现，避免测试与脚本出现两套检索逻辑。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

EVAL_DIR = Path(__file__).resolve().parent
RUN_EVAL_PATH = EVAL_DIR / "run_eval.py"

# 与 perf 共享 testcontainers 栈；support 位于 tests/support。
TESTS_DIR = EVAL_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

REQUIRED_CATEGORIES = {
    "single_hop",
    "multi_hop",
    "temporal",
    "contradiction",
    "negation",
    "scope",
    "procedural",
}


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("golden_run_eval", RUN_EVAL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 在 ``from __future__ import annotations`` 下会按模块名反查
    # sys.modules；不注册会在解析 EvalReport 字段时抛 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def run_eval_module() -> ModuleType:
    return _load_module()


def test_golden_set_has_required_coverage(run_eval_module: ModuleType) -> None:
    queries = run_eval_module.load_queries()
    memories = run_eval_module.load_memories()
    categories = {query["category"] for query in queries}

    assert REQUIRED_CATEGORIES <= categories
    assert len(memories) >= 200
    assert len(queries) >= 100

    counts = {category: 0 for category in REQUIRED_CATEGORIES}
    for query in queries:
        counts[query["category"]] += 1
    assert min(counts.values()) >= 8, counts


def test_golden_set_is_reviewable_and_covers_required_cases(
    run_eval_module: ModuleType,
) -> None:
    memories = run_eval_module.load_memories()
    queries = run_eval_module.load_queries()
    contents = [memory["content"] for memory in memories]

    # 不是 200 条复制粘贴：正文去重率至少 95%，且每条都有可读正文。
    assert len(set(contents)) >= int(len(contents) * 0.95)
    assert all(len(content.strip()) >= 6 for content in contents)

    # 矛盾类必须覆盖“北京 → 上海”，且旧事实可追溯到替代它的新事实。
    by_key = {memory["key"]: memory for memory in memories}
    contradiction_pairs = [
        memory
        for memory in memories
        if memory.get("superseded_by_key")
    ]
    assert any(
        "北京" in memory["content"]
        and "上海" in by_key[memory["superseded_by_key"]]["content"]
        for memory in contradiction_pairs
    )

    # 否定类必须包含“不吃辣”，查询里也要真的问到这个事实。
    assert any("不吃辣" in memory["content"] for memory in memories)
    assert any("辣" in query["query"] for query in queries)

    stale_queries = [query for query in queries if query.get("stale_keys")]
    assert len(stale_queries) >= 10


def test_eval_meets_m1_baseline(golden_env, run_eval_module: ModuleType) -> None:
    report = run_eval_module.run_eval(
        provider="fake", env=golden_env, write_report=False
    )

    assert report.recall_at_5 >= 0.80
    assert report.stale_fact_rate <= 0.05
    assert report.mrr >= 0.60
    assert report.contradiction_accuracy >= 0.80
    assert report.avg_tokens > 0
