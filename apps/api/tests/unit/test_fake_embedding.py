"""确定性 FakeEmbedding 的检索可用性约束。"""

from __future__ import annotations

import math

from app.adapters.embeddings import FakeEmbedding


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def test_fake_embedding_prefers_shared_terms() -> None:
    """共享查询词的记忆必须比无关记忆更接近查询；黄金集基线依赖此性质。"""
    provider = FakeEmbedding(dim=64)
    query, target, distractor = provider.embed_sync(
        [
            "林舟现在住在哪个城市？",
            "林舟住在上海，通勤到张江大约四十分钟。",
            "宋雨喜欢手冲咖啡，常买云南产区的豆子。",
        ]
    )

    assert math.isclose(_cosine(query, target), _cosine(target, query))
    assert _cosine(query, target) > _cosine(query, distractor)
    assert _cosine(query, target) > 0.10


def test_fake_embedding_stays_deterministic_and_normalized() -> None:
    provider = FakeEmbedding(dim=16)
    first = provider.embed_sync(["Atlas 使用 PostgreSQL 作为事实源"])[0]
    second = provider.embed_sync(["Atlas 使用 PostgreSQL 作为事实源"])[0]

    assert first == second
    assert math.isclose(sum(value * value for value in first), 1.0, rel_tol=1e-9)
