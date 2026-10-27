"""记忆写入 schema 与内容规范化。

内容校验放在 schema 层，保证 ``normalize_content`` 既能被 REST 路由复用，
也能被后续的抽取/导入管线直接调用。校验失败统一抛 :class:`ValidationError`，
由全局错误处理器转成 ``422`` 与稳定的错误码。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.errors import ApiError
from memory_core.types import MemoryType, Scope

MAX_CONTENT_CHARS = 8192

_BLANK_LINES = re.compile(r"\n{3,}")
_EMPTY_LINE_SPACES = re.compile(r"[ \t]+\n")


class ValidationError(ApiError):
    """写入参数不合法；默认转为 422。"""

    status_code = 422
    code = "validation_error"
    default_message = "记忆内容不合法"


def normalize_content(raw: str, max_chars: int = MAX_CONTENT_CHARS) -> str:
    """规范化记忆正文。

    - 去首尾空白，统一换行符；
    - 删除行尾空白，把连续空行压缩为一个空行；
    - 规范化后为空、含 NUL/孤立代理对、或超过 ``max_chars`` 字符时抛
      :class:`ValidationError`。

    ``\\x07``、ESC 等可被 PostgreSQL 存储的控制字符按原样保留，保证写入/读取
    可逆；只有 NUL 与无法编码为 UTF-8 的代理对会被拒绝。
    """
    if not isinstance(raw, str):
        raise ValidationError(
            "content 必须是字符串", details={"field": "content"}
        )

    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = _EMPTY_LINE_SPACES.sub("\n", text)
    text = _BLANK_LINES.sub("\n\n", text)

    if not text:
        raise ValidationError(
            "content 不能为空", details={"field": "content"}
        )

    invalid = _invalid_character(text)
    if invalid is not None:
        raise ValidationError(
            "content 包含无法存储的字符",
            code="invalid_character",
            details={"field": "content", "character": invalid},
        )

    if len(text) > max_chars:
        raise ValidationError(
            f"content 超过 {max_chars} 字符，请拆分后重试",
            code="content_too_long",
            details={"field": "content", "max_chars": max_chars},
        )

    return text


def _invalid_character(text: str) -> str | None:
    """返回第一个无法安全写入 PostgreSQL text 的字符转义；没有则返回 None。"""
    for char in text:
        code = ord(char)
        if char == "\x00":
            return "\\u0000"
        if 0xD800 <= code <= 0xDFFF:
            return f"\\u{code:04x}"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:  # pragma: no cover - 上面已覆盖代理对
        return f"\\u{ord(text[exc.start]):04x}"
    return None


class EntitySpec(BaseModel):
    """调用方显式提供的实体。"""

    name: str = Field(min_length=1, max_length=255)
    type: str | None = Field(default=None, max_length=64)
    role: str | None = Field(default=None, max_length=32)


class SourceSpec(BaseModel):
    """记忆来源；M1 未归档原文，只保留来源类型、id 与原话。"""

    type: str | None = Field(default=None, max_length=32)
    id: str | None = Field(default=None, max_length=64)
    quote: str | None = Field(default=None, max_length=2000)


class MemoryWriteRequest(BaseModel):
    """写入一条记忆的请求。

    ``workspace_id`` 可省略，由路由按认证主体解析后补全；服务层要求它最终
    非空。``importance`` 是人工/LLM 基线分，规则信号仍会在此基础上修正。
    """

    content: str
    type: MemoryType = MemoryType.SEMANTIC
    scope: Scope = Scope.USER
    scope_id: str | None = Field(default=None, max_length=36)
    workspace_id: str | None = Field(default=None, max_length=36)
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    entities: list[EntitySpec] = Field(default_factory=list)
    source: SourceSpec | None = None
    ttl_seconds: int | None = Field(default=None, gt=0)
    dedupe: bool = True
    temporal_change: bool = False
    idempotency_key: str | None = Field(default=None, max_length=200)


def validation_details(field: str, **extra: Any) -> dict[str, Any]:
    """构造统一的字段级错误详情。"""
    return {"field": field, **extra}
