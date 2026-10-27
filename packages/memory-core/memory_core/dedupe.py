"""去重与冲突决策：把相似度候选集映射为确定的写入动作。

规则来自规格 5.3，按优先级执行：

1. 最高相似度 ``≥ merge_threshold``：近重复，合并到该候选；
2. 最高相似度低于 ``conflict_threshold``：内容无关，直接新建；
3. ``temporal_change``：时间语义不同，新旧共存（旧条目仅关闭有效期）；
4. ``contradiction=True``：明确矛盾，新条目取代最相似的候选；
5. 其余：直接新建。

本模块是纯决策函数：LLM 判定由上层完成，只把 ``contradiction`` 作为输入传入；
函数不访问 DB、网络或模型，也不修改任何候选对象。已带 ``valid_to`` 的候选
视为有效期已关闭（上层候选集本应只含 active 记忆），不参与决策。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

DEFAULT_MERGE_THRESHOLD = 0.92
DEFAULT_CONFLICT_THRESHOLD = 0.75


class ConflictKind(str, Enum):
    """冲突决策的四种结果。"""

    CREATE = "create"
    MERGE = "merge"
    SUPERSEDE = "supersede"
    COEXIST = "coexist"


@dataclass(frozen=True, slots=True)
class ExistingCandidate:
    """一条已有记忆及其与新内容的相似度。

    ``subject_predicate`` 供上层做同主谓筛选或审计，本模块不据此改变决策；
    ``valid_to`` 非空表示有效期已关闭。
    """

    id: str
    content: str
    similarity: float
    subject_predicate: tuple[str, str] | None = None
    valid_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class ConflictDecision:
    """一次写入决策，``target_id`` 为被合并/取代/共存的已有记忆。"""

    kind: ConflictKind
    target_id: str | None
    reason: str


def decide_conflict(
    new_content: str,
    candidates: Sequence[ExistingCandidate],
    *,
    contradiction: bool | None,
    temporal_change: bool = False,
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
    conflict_threshold: float = DEFAULT_CONFLICT_THRESHOLD,
) -> ConflictDecision:
    """按规格 5.3 的优先级决定 ``new_content`` 的写入方式。

    ``new_content`` 是待写入内容；相似度已由上层计算并放进候选集，本函数只依据
    候选集决策。``contradiction`` 为 ``None`` 表示上层未做或未完成矛盾判定，
    此时只有近重复会合并，其余一律新建。相似度最高的候选并列时取候选序列中
    靠前者，保证结果确定。每个分支都返回可审计的 ``reason``。
    """
    active = [candidate for candidate in candidates if candidate.valid_to is None]
    if not active:
        if candidates:
            return ConflictDecision(
                ConflictKind.CREATE,
                None,
                "候选均已关闭有效期，无可合并或取代的目标，直接新建",
            )
        return ConflictDecision(
            ConflictKind.CREATE,
            None,
            "无相似度候选，直接新建",
        )

    best = max(active, key=lambda candidate: candidate.similarity)

    if best.similarity >= merge_threshold:
        return ConflictDecision(
            ConflictKind.MERGE,
            best.id,
            f"最高相似度 {best.similarity:.3f} ≥ 合并阈值 {merge_threshold:.3f}，"
            f"视为近重复，合并到 {best.id}",
        )

    if best.similarity < conflict_threshold:
        return ConflictDecision(
            ConflictKind.CREATE,
            None,
            f"最高相似度 {best.similarity:.3f} < 冲突阈值 {conflict_threshold:.3f}，"
            f"内容无关，直接新建",
        )

    if temporal_change:
        return ConflictDecision(
            ConflictKind.COEXIST,
            best.id,
            f"最高相似度 {best.similarity:.3f} 且时间语义不同，"
            f"与 {best.id} 共存并关闭旧条目有效期",
        )

    if contradiction:
        return ConflictDecision(
            ConflictKind.SUPERSEDE,
            best.id,
            f"最高相似度 {best.similarity:.3f} 且判定为明确矛盾，"
            f"新条目取代 {best.id}（valid_to=now, status=superseded）",
        )

    return ConflictDecision(
        ConflictKind.CREATE,
        None,
        f"最高相似度 {best.similarity:.3f} 未达合并阈值且无明确矛盾，直接新建",
    )
