"""重要性评分：LLM 基线经规则信号修正，再按记忆类型设上限。

评分顺序固定为：基线（缺失时取中性值 0.5）→ 规则修正（显式记住、置顶、
helpful 反馈）→ 类型上限（工作记忆 ≤ 0.5）→ 裁剪到 ``[0, 1]``。纯领域逻辑，
不依赖任何 IO。
"""

from __future__ import annotations

from dataclasses import dataclass

from memory_core.types import MemoryType

#: LLM 未给出分数时的中性基线。
NEUTRAL_LLM_SCORE = 0.5

#: 用户显式要求记住时的最低分。
EXPLICIT_REMEMBER_FLOOR = 0.9

#: 用户手动置顶时的固定分。
PINNED_SCORE = 1.0

#: 每次 ``helpful`` 反馈带来的增量。
HELPFUL_FEEDBACK_STEP = 0.05

#: 各记忆类型的分数上限；未列出的类型不设额外上限。
TYPE_SCORE_CAPS: dict[MemoryType, float] = {
    MemoryType.WORKING: 0.5,
}


@dataclass(frozen=True, slots=True)
class ImportanceSignals:
    """评分时可用的确定性信号，来自用户行为而非 LLM。"""

    explicit_remember: bool = False
    pinned: bool = False
    helpful_feedback: int = 0


def _clamp(score: float) -> float:
    """把分数裁剪到闭区间 ``[0, 1]``。"""
    return min(1.0, max(0.0, score))


def effective_llm_score(
    llm_score: float | None,
    signals: ImportanceSignals,
) -> float:
    """把 LLM 基线分与规则信号合成为有效分，不含类型上限。

    ``llm_score`` 为 ``None`` 时从中性基线开始；显式记住只抬升下限，置顶直接
    覆盖为 1.0，helpful 反馈每次加 0.05（负数视为 0 次）。结果裁剪到
    ``[0, 1]``。
    """
    score = NEUTRAL_LLM_SCORE if llm_score is None else llm_score

    if signals.explicit_remember:
        score = max(score, EXPLICIT_REMEMBER_FLOOR)
    if signals.pinned:
        score = PINNED_SCORE
    score += HELPFUL_FEEDBACK_STEP * max(signals.helpful_feedback, 0)

    return _clamp(score)


def score_importance(
    content: str,
    memory_type: MemoryType,
    signals: ImportanceSignals,
    llm_score: float | None,
) -> float:
    """计算一条记忆的最终重要性，返回 ``[0, 1]`` 内的浮点数。

    在 :func:`effective_llm_score` 的基础上应用类型上限：工作记忆只服务当前
    会话，即使被置顶或显式记住也不超过 0.5。``content`` 预留给后续基于文本
    的规则，当前不参与计算。
    """
    score = effective_llm_score(llm_score, signals)

    cap = TYPE_SCORE_CAPS.get(memory_type)
    if cap is not None:
        score = min(score, cap)

    return _clamp(score)
