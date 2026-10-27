"""RRF 融合与最终排序分数。

规格 6.2：多路召回先按 ``score = Σ 1/(k + rank_i)`` 融合（``k=60``），
再乘重要性权重与作用域权重。本模块只做纯计算，不接触 DB、网络或模型。
"""

from __future__ import annotations

from memory_core.types import Scope

#: RRF 的平滑常数；越大越弱化名次差异。
DEFAULT_RRF_K = 60

#: 作用域权重：越具体的作用域越优先。
SCOPE_WEIGHTS: dict[Scope, float] = {
    Scope.SESSION: 1.0,
    Scope.AGENT: 0.98,
    Scope.USER: 0.96,
    Scope.GLOBAL: 0.94,
}


def rrf(rankings: dict[str, list[str]], k: int = DEFAULT_RRF_K) -> dict[str, float]:
    """把多路排名的记忆 ID 融合为 RRF 分数。

    ``rankings`` 的键是通道名，值是从第 1 名开始的 ID 列表；同一 ID 出现在多个
    通道会累加贡献，这正是 RRF 奖励“多路共识”的方式。同一通道内重复出现的 ID
    只计第一次名次，避免异常输入放大分数。名次计数从 1 开始，因此 ``k=0`` 仍然
    合法；负 ``k`` 可能除以零或产生负分，直接拒绝。
    """
    if k < 0:
        raise ValueError(f"RRF 的 k 不能为负数：{k}")

    scores: dict[str, float] = {}
    for ranking in rankings.values():
        seen: set[str] = set()
        for rank, memory_id in enumerate(ranking, start=1):
            if memory_id in seen:
                continue
            seen.add(memory_id)
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (k + rank)
    return scores


def final_score(rrf_score: float, importance: float, scope: Scope | str) -> float:
    """按规格 6.2 计算最终分数：``rrf × (0.8 + 0.2×importance) × scope_weight``。

    ``scope`` 同时接受 :class:`Scope` 与等值字符串；未知作用域抛 ``ValueError``，
    避免静默使用错误的权重。程序性记忆的成功率加成由检索服务在调用本函数后
    另行相乘，本函数不感知记忆类型。
    """
    return rrf_score * (0.8 + 0.2 * importance) * SCOPE_WEIGHTS[Scope(scope)]
