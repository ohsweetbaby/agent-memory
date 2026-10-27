"""去重与冲突决策：把相似度候选集映射为确定的写入动作。

规则来自规格 5.3，按优先级执行：

1. 最高相似度 ``≥ merge_threshold``：近重复，合并到该候选；
2. 最高相似度低于 ``conflict_threshold``：内容无关，直接新建；
3. ``temporal_change``：时间语义不同，新旧共存（旧条目仅关闭有效期）；
4. ``contradiction=True``：明确矛盾，新条目取代最相似的候选；
5. 其余：直接新建。

本模块是纯决策函数：LLM 判定由上层完成，只把 ``contradiction`` 作为输入传入；
函数不访问 DB、网络或模型，也不修改任何候选对象。**调用方只传 active 候选**，
本模块不依据 ``valid_to`` 过滤（该字段仅供调用方表达有效期）。

若传入 ``new_subject_predicate``，双方都有主谓且不一致的候选不参与
MERGE / SUPERSEDE / COEXIST；新内容未提供主谓时不做额外过滤，保持兼容。
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

    ``subject_predicate`` 供同主谓校验与审计；``valid_to`` 仅供调用方表达有效期，
    本模块不做过滤（调用方应只传 active 候选）。
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


def _subject_predicate_compatible(
    new_subject_predicate: tuple[str, str] | None,
    candidate_subject_predicate: tuple[str, str] | None,
) -> bool:
    """双方都有主谓时才要求相等；任一方缺失则不额外过滤。"""
    if new_subject_predicate is None or candidate_subject_predicate is None:
        return True
    return new_subject_predicate == candidate_subject_predicate


def decide_conflict(
    new_content: str,
    candidates: Sequence[ExistingCandidate],
    *,
    contradiction: bool | None,
    temporal_change: bool = False,
    new_subject_predicate: tuple[str, str] | None = None,
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
    conflict_threshold: float = DEFAULT_CONFLICT_THRESHOLD,
) -> ConflictDecision:
    """按规格 5.3 的优先级决定 ``new_content`` 的写入方式。

    ``new_content`` 是待写入内容；相似度已由上层计算并放进候选集，本函数只依据
    候选集决策。``contradiction`` 为 ``None`` 表示上层未做或未完成矛盾判定，
    此时只有近重复会合并，其余一律新建。相似度最高的候选并列时取候选序列中
    靠前者，保证结果确定。每个分支都返回可审计的 ``reason``。

    ``new_subject_predicate`` 可选。提供后，双方主谓都存在且不一致的候选会被
    排除在 MERGE / SUPERSEDE / COEXIST 之外；任一方主谓缺失时该候选仍参与决策。
    候选是否 active 由调用方保证，本函数不看 ``valid_to``。
    """
    eligible = [
        candidate
        for candidate in candidates
        if _subject_predicate_compatible(new_subject_predicate, candidate.subject_predicate)
    ]
    if not eligible:
        if candidates:
            return ConflictDecision(
                ConflictKind.CREATE,
                None,
                "候选主谓均与待写入内容不一致，无可合并或取代的目标，直接新建",
            )
        return ConflictDecision(
            ConflictKind.CREATE,
            None,
            "无相似度候选，直接新建",
        )

    best = max(eligible, key=lambda candidate: candidate.similarity)

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
