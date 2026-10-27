"""艾宾浩斯遗忘曲线：稳定性、保留率与归档判定。

公式来自规格 5.4：

``R = exp(-Δt / S)``
``S = S0 × (1 + α×importance) × (1 + β×ln(1 + access_count)) × rehearsal_factor``

纯领域逻辑，不依赖 DB 或网络。所有 datetime 参数必须带时区，未来时间戳按
``Δt = 0`` 处理。只有 ``R`` 严格小于归档阈值时才归档，等于阈值不归档。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

_SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True, slots=True)
class DecayParams:
    """遗忘曲线参数；默认值来自规格 5.4 与配置项 ``decay.*``。"""

    base_stability_days: float = 7.0
    archive_threshold: float = 0.15
    importance_alpha: float = 2.0
    access_beta: float = 0.8
    rehearsal_factor: float = 1.5
    max_rehearsal_factor: float = 10.0


def stability_days(
    importance: float,
    access_count: int,
    rehearsal_factor: float,
    params: DecayParams,
) -> float:
    """计算记忆的稳定性 ``S``，单位天。

    重要性越高、被访问次数越多、复习次数越多的记忆，稳定性越大、衰减越慢。
    非正的 ``rehearsal_factor`` 视为从未复习（1.0），避免脏数据把稳定性清零；
    负数 ``access_count`` 同样按 0 处理。
    """
    effective_rehearsal = rehearsal_factor if rehearsal_factor > 0 else 1.0

    return (
        params.base_stability_days
        * (1.0 + params.importance_alpha * importance)
        * (1.0 + params.access_beta * math.log1p(max(access_count, 0)))
        * effective_rehearsal
    )


def retention(
    last_reinforced_at: datetime,
    now: datetime,
    stability_days: float,
) -> float:
    """按距上次强化的天数计算保留率 ``R``，返回 ``[0, 1]`` 内的浮点数。

    未来时间戳按 ``Δt = 0`` 处理，此时保留率为 1.0。稳定性非正时视为已经
    完全衰减（零时刻除外，避免除零）。
    """
    elapsed_days = max(
        (now - last_reinforced_at).total_seconds() / _SECONDS_PER_DAY,
        0.0,
    )

    if stability_days <= 0:
        return 1.0 if elapsed_days == 0 else 0.0

    return math.exp(-elapsed_days / stability_days)


def should_archive(
    last_reinforced_at: datetime,
    now: datetime,
    importance: float,
    access_count: int,
    rehearsal_factor: float,
    params: DecayParams,
) -> bool:
    """判断一条 active 记忆是否应因保留率过低而归档。

    仅当 ``R < archive_threshold`` 时返回 ``True``；相等时不归档。
    """
    stability = stability_days(importance, access_count, rehearsal_factor, params)
    return retention(last_reinforced_at, now, stability) < params.archive_threshold


def apply_rehearsal(current: float, params: DecayParams) -> float:
    """返回一次有效复习后的复习系数，按 ``rehearsal_factor`` 倍增长并封顶。

    非正的 ``current`` 视为初始值 1.0；非正的 ``rehearsal_factor`` 视为不放大。
    """
    base = current if current > 0 else 1.0
    step = params.rehearsal_factor if params.rehearsal_factor > 0 else 1.0
    return min(base * step, params.max_rehearsal_factor)
