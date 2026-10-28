"""上下文组装：分组、近重复折叠与按 token 预算截断。

规格 6.3：默认预算 1500 tokens，分组顺序为语义事实 → 程序性技能 → 情景事件 →
归档/历史；每条格式为 ``[类型|时间|置信度|来源] 内容``，来源为消息时附原话
（≤ 200 字）。本模块是纯领域逻辑，不依赖 DB、网络或模型。

由于 :class:`ContextItem` 可以携带检索的 final 分数，组内排序与近重复保留都
优先使用 ``score``；未提供时回退到 ``importance × confidence × retention``
作为上下文优先级。近重复判定采用文本近似规则：包含关系、仅插入 ≤ 2 个字符的
变体，或编辑距离比 ≤ 0.2。规格中的向量余弦去重需要嵌入，留给上层在构造条目
时完成。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from memory_core.types import MemoryType, Scope

#: 默认上下文预算（token）。
DEFAULT_CONTEXT_BUDGET_TOKENS = 1500

#: 来源原话最多保留的字符数。
MAX_SOURCE_QUOTE_CHARS = 200

#: 编辑距离比不超过该值即视为近似重复。
NEAR_DUPLICATE_EDIT_RATIO = 0.2

#: 允许把“较短内容仅插入少量字符”视为包含关系的最大插入字符数。
_MAX_INSERTION_VARIANT_CHARS = 2

#: 分组顺序：语义 → 程序性 → 情景；其余类型（含 archived/history）排最后。
_GROUP_ORDER: dict[str, int] = {
    "semantic": 0,
    "procedural": 1,
    "episodic": 2,
}
_LAST_GROUP = 3

#: 上下文中的类型标签。
_TYPE_LABELS: dict[str, str] = {
    "working": "工作",
    "episodic": "情景",
    "semantic": "语义",
    "procedural": "程序性",
    "perceptual": "感知",
    "archived": "归档",
    "history": "历史",
}

#: 来源为消息时的标签；缺失来源统一显示“未知”。
_MESSAGE_SOURCE_LABEL = "消息"
_UNKNOWN_SOURCE_LABEL = "未知"


@dataclass(frozen=True, slots=True)
class ContextItem:
    """注入提示词的单条记忆。

    ``type`` 接受 :class:`MemoryType` 或等值字符串；``why`` 保留检索阶段的
    解释信息，组装逻辑不解释其内容。``source_quote`` 是消息原文片段，仅在
    格式化时截断，不会修改条目本身。``historical`` 标记归档或被取代的历史
    条目：它们保留真实类型标签，但统一排在所有常规分组之后。``score`` 是检索
    的 final 分数，提供时组内排序与去重都以它为准。
    """

    id: str
    type: MemoryType | str
    content: str
    importance: float
    confidence: float
    retention: float
    timestamp: datetime
    scope: Scope
    source_quote: str | None = None
    why: dict[str, object] | None = None
    historical: bool = False
    score: float | None = None


def estimate_tokens(text: str) -> int:
    """按 ``max(1, ceil(len(text)/2))`` 估算 token 数。

    这是与检索服务共享的粗略口径：中文场景下约每两个字符一个 token。空文本
    仍返回 1，保证任何非空预算下的计算都有下界。
    """
    return max(1, math.ceil(len(text) / 2))


def assemble_context(
    items: list[ContextItem],
    budget_tokens: int = DEFAULT_CONTEXT_BUDGET_TOKENS,
) -> tuple[str, list[ContextItem]]:
    """把候选条目组装成可直接注入提示词的文本。

    处理顺序：近重复折叠（保留分数高者）→ 按分组与分数排序 → 贪心装入预算。
    放不进预算的条目跳过而不是截断内容，避免把半句话当作事实注入；后排名但更短
    的条目仍有机会进入预算。返回 ``(context_text, kept_items)``，``kept_items``
    的顺序与文本一致。
    """
    if budget_tokens < 0:
        raise ValueError(f"上下文预算不能为负数：{budget_tokens}")
    if not items or budget_tokens == 0:
        return "", []

    ordered = sorted(
        _collapse_near_duplicates(items),
        key=lambda item: (_group(item), -_priority(item), item.id),
    )

    lines: list[str] = []
    kept: list[ContextItem] = []
    for item in ordered:
        line = _format_item(item)
        candidate = "\n".join([*lines, line])
        if estimate_tokens(candidate) > budget_tokens:
            continue
        lines.append(line)
        kept.append(item)
    return "\n".join(lines), kept


def _type_key(type_: MemoryType | str) -> str:
    """把类型枚举或字符串统一成字符串键。"""
    return type_.value if isinstance(type_, MemoryType) else str(type_)


def _group(item: ContextItem) -> int:
    """返回条目的分组序号；归档/历史条目不关心类型，统一排最后。"""
    if item.historical:
        return _LAST_GROUP
    return _GROUP_ORDER.get(_type_key(item.type), _LAST_GROUP)


def _priority(item: ContextItem) -> float:
    """组内排序分数：显式 final 分数优先，否则用三项乘积作为代理分数。"""
    if item.score is not None:
        return item.score
    return item.importance * item.confidence * item.retention


def _format_item(item: ContextItem) -> str:
    """格式化为 ``[类型|时间|置信度|来源] 内容``，有原话时追加片段。"""
    key = _type_key(item.type)
    label = _TYPE_LABELS.get(key, key)
    timestamp = item.timestamp.strftime("%Y-%m-%d")
    quote = (item.source_quote or "").strip()

    if quote:
        source = _MESSAGE_SOURCE_LABEL
        quote = quote[:MAX_SOURCE_QUOTE_CHARS]
    else:
        source = _UNKNOWN_SOURCE_LABEL

    line = f"[{label}|{timestamp}|{item.confidence:.2f}|{source}] {item.content.strip()}"
    if quote:
        line += f"（原话：{quote}）"
    return line


def _collapse_near_duplicates(items: list[ContextItem]) -> list[ContextItem]:
    """去掉近似重复条目，保留分数高者。

    先按 ``_priority`` 降序（显式 score 优先，同分时内容更长者优先，再按 ID
    保证确定性）遍历，因此任何被保留的条目都不会被后续低分条目替换，确保
    “只保留分数高者”。
    """
    ordered = sorted(
        items,
        key=lambda item: (-_priority(item), -len(item.content), item.id),
    )
    kept: list[ContextItem] = []
    for item in ordered:
        if any(_is_near_duplicate(item.content, existing.content) for existing in kept):
            continue
        kept.append(item)
    return kept


def _normalize(content: str) -> str:
    """去掉所有空白并折叠大小写，降低格式差异对近重复判定的干扰。"""
    return "".join(content.split()).casefold()


def _is_near_duplicate(left: str, right: str) -> bool:
    """内容包含、仅插入少量修饰词、或编辑距离比 ≤ 0.2 时视为近似重复。

    仅用 ``编辑距离 / 较长长度 ≤ 0.2`` 会漏掉任务要求的例子
    “用户对花生过敏” → “用户对花生严重过敏”（插入 2 字，2/9 ≈ 0.22），
    因此把“包含关系”扩展为“较短内容仅靠插入 ≤ 2 个字符即可得到较长内容”。
    替换类差异仍走严格比例，避免把“喜欢摇滚”和“喜欢爵士”这类不同事实折叠。
    少于 4 字的短内容只认包含关系，避免误判两个不相干的短词。
    """
    a = _normalize(left)
    b = _normalize(right)
    if not a or not b:
        return a == b
    # 否定词是事实语义的分水岭，不能当作“插入的修饰词”折叠。
    if _has_negation(a) != _has_negation(b):
        return False
    if a in b or b in a:
        return True
    if min(len(a), len(b)) < 4:
        return False
    # 等长且只差一个字时，中文里往往就是完全不同的事实（北京/南京）。
    if len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1:
        return False

    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(longer) - len(shorter) <= _MAX_INSERTION_VARIANT_CHARS and _is_subsequence(
        shorter, longer
    ):
        return True

    return _edit_distance(a, b) / len(longer) <= NEAR_DUPLICATE_EDIT_RATIO


_NEGATION_MARKERS = ("不", "没", "无", "未", "别", "非")


def _has_negation(text: str) -> bool:
    return any(marker in text for marker in _NEGATION_MARKERS)


def _is_subsequence(shorter: str, longer: str) -> bool:
    """判断 ``shorter`` 是否为 ``longer`` 的子序列（只允许插入/删除）。"""
    remaining = iter(longer)
    return all(char in remaining for char in shorter)


def _edit_distance(left: str, right: str) -> int:
    """Levenshtein 距离；只需保留前一行，空间 O(min(len))。"""
    if left == right:
        return 0
    if len(left) < len(right):
        left, right = right, left
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]
