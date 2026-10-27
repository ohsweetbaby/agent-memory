"""写入前编码：规范化、PII 标记、重要性与实体。

M1 的写路径不等待嵌入或 LLM：``encode_memory`` 默认只做确定性处理。
``llm`` 参数预留给任务 14 的抽取管线，当前不发起任何同步调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from memory_core.importance import ImportanceSignals, score_importance
from memory_core.types import MemoryType, Scope

from app.models.base import utcnow
from app.schemas.memory import (
    EntitySpec,
    MemoryWriteRequest,
    ValidationError,
    normalize_content,
    validation_details,
)

#: 规格 §4.1 的默认重要性；LLM/人工未给分时作为基线。
TYPE_DEFAULT_IMPORTANCE: dict[MemoryType, float] = {
    MemoryType.WORKING: 0.3,
    MemoryType.EPISODIC: 0.5,
    MemoryType.SEMANTIC: 0.6,
    MemoryType.PROCEDURAL: 0.6,
    MemoryType.PERCEPTUAL: 0.5,
}

#: 用户显式要求记住的短语；命中后走 ``ImportanceSignals.explicit_remember``。
EXPLICIT_REMEMBER_MARKERS = ("记住", "别忘了", "请记住", "记得")

PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "id_card": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
}

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")


@dataclass(frozen=True, slots=True)
class EncodedEntity:
    """编码后的实体；同工作区内按 name 去重。"""

    name: str
    type: str | None = None
    role: str | None = None


@dataclass(frozen=True, slots=True)
class EncodedMemory:
    """可直接落库的编码结果。"""

    content: str
    type: MemoryType
    scope: Scope
    scope_id: str | None
    workspace_id: str
    importance: float
    confidence: float
    stability: float
    metadata: dict[str, Any] = field(default_factory=dict)
    entities: tuple[EncodedEntity, ...] = ()
    source_type: str = "manual"
    source_id: str | None = None
    valid_from: datetime = field(default_factory=utcnow)
    valid_to: datetime | None = None


def detect_pii(content: str) -> list[str]:
    """返回命中的 PII 类型（去重排序）；只做标记，不改写内容。"""
    return sorted(
        name for name, pattern in PII_PATTERNS.items() if pattern.search(content)
    )


def detect_language(content: str) -> str | None:
    """粗略识别中/英/混排，用于后续选择分词器与嵌入模型。"""
    has_cjk = bool(_CJK.search(content))
    has_latin = bool(_LATIN.search(content))
    if has_cjk and has_latin:
        return "mixed"
    if has_cjk:
        return "zh"
    if has_latin:
        return "en"
    return None


def has_explicit_remember(content: str) -> bool:
    """内容里是否出现“记住/别忘了/请记住”等显式要求。"""
    return any(marker in content for marker in EXPLICIT_REMEMBER_MARKERS)


def _normalize_entities(entities: list[EntitySpec]) -> tuple[EncodedEntity, ...]:
    seen: set[str] = set()
    result: list[EncodedEntity] = []
    for entity in entities:
        name = entity.name.strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(
            EncodedEntity(
                name=name,
                type=entity.type.strip() if entity.type else None,
                role=entity.role.strip() if entity.role else None,
            )
        )
    return tuple(result)


def _resolve_scope_id(scope: Scope, scope_id: str | None) -> str | None:
    normalized = scope_id.strip() if isinstance(scope_id, str) else None
    if scope is Scope.GLOBAL:
        if normalized:
            raise ValidationError(
                "global 作用域不能指定 scope_id",
                details=validation_details("scope_id"),
            )
        return None
    if not normalized:
        raise ValidationError(
            f"{scope.value} 作用域必须指定 scope_id",
            details=validation_details("scope_id"),
        )
    return normalized


def encode_memory(
    req: MemoryWriteRequest, llm: Any | None = None
) -> EncodedMemory:
    """把请求编码成可落库结构。

    ``llm`` 仅作为未来扩展点保留；M1 不同步调用模型，确保写入 p95 目标。
    """
    del llm  # 显式表明当前不调用

    if not req.workspace_id:
        raise ValidationError(
            "必须指定 workspace_id", details=validation_details("workspace_id")
        )

    content = normalize_content(req.content)
    scope_id = _resolve_scope_id(req.scope, req.scope_id)
    now = datetime.now(UTC)

    metadata: dict[str, Any] = {}
    pii_types = detect_pii(content)
    if pii_types:
        # 规格 §5.1 允许后续脱敏；M1 只标记，避免破坏原文可逆性。
        metadata["pii"] = True
        metadata["pii_types"] = pii_types
        metadata["pii_fields"] = ["content"]

    language = detect_language(content)
    if language is not None:
        metadata["language"] = language

    explicit_remember = has_explicit_remember(content)
    signals = ImportanceSignals(explicit_remember=explicit_remember)
    baseline = (
        req.importance
        if req.importance is not None
        else TYPE_DEFAULT_IMPORTANCE.get(req.type, 0.5)
    )
    importance = score_importance(content, req.type, signals, baseline)
    if explicit_remember:
        metadata["importance_reason"] = "explicit_remember"
    elif req.importance is not None:
        metadata["importance_reason"] = "manual"
    else:
        metadata["importance_reason"] = "type_default"

    entities = _normalize_entities(req.entities)
    if entities:
        metadata["entities"] = [entity.name for entity in entities]

    source_type = "manual"
    source_id: str | None = None
    if req.source is not None:
        source_type = (req.source.type or "manual").strip() or "manual"
        source_id = req.source.id.strip() if req.source.id else None
        if req.source.quote:
            # 规格 §4.4：来源原话最多 200 字。
            metadata["source_quote"] = req.source.quote.strip()[:200]

    valid_to = None
    if req.ttl_seconds is not None:
        valid_to = now + timedelta(seconds=req.ttl_seconds)

    return EncodedMemory(
        content=content,
        type=req.type,
        scope=req.scope,
        scope_id=scope_id,
        workspace_id=req.workspace_id,
        importance=importance,
        confidence=0.5,
        stability=1.0,
        metadata=metadata,
        entities=entities,
        source_type=source_type,
        source_id=source_id,
        valid_from=now,
        valid_to=valid_to,
    )


__all__ = [
    "EncodedEntity",
    "EncodedMemory",
    "ValidationError",
    "detect_language",
    "detect_pii",
    "encode_memory",
    "has_explicit_remember",
    "normalize_content",
]
