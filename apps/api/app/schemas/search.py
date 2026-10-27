"""检索请求、响应与统一错误约定。

``why`` 保留四路召回的原始名次与 RRF 中间值，便于管理台调试；响应只暴露
面向调用方的安全字段，不返回完整内部元数据或任何密钥。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from memory_core.types import MemoryType, Scope

SearchMode = Literal["balanced", "recent", "deep", "exact"]

MAX_QUERY_CHARS = 1000
MAX_SEARCH_LIMIT = 50


class TimeRange(BaseModel):
    """检索时间范围；``from`` 是 Python 关键字，使用别名对外暴露。"""

    model_config = ConfigDict(populate_by_name=True)

    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None


class SearchRequest(BaseModel):
    """四路召回的统一入参；``workspace_id`` 由路由按认证主体补齐。"""

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    types: list[MemoryType] | None = None
    scopes: list[Scope] | None = None
    workspace_id: str | None = Field(default=None, max_length=36)
    agent_id: str | None = Field(default=None, max_length=36)
    end_user_id: str | None = Field(default=None, max_length=36)
    session_id: str | None = Field(default=None, max_length=36)
    time_range: TimeRange | None = None
    limit: int = Field(default=8, ge=1, le=MAX_SEARCH_LIMIT)
    mode: SearchMode = "balanced"
    include_history: bool = False
    include_archived: bool = False

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("query 不能为空")
        return text


class SearchItem(BaseModel):
    """单条召回结果；``score`` 是加权后的 final 分数。"""

    id: str
    type: str
    scope: str
    scope_id: str | None = None
    content: str
    importance: float
    confidence: float
    retention: float
    status: str
    historical: bool = False
    valid_from: datetime
    timestamp: datetime
    superseded_by: str | None = None
    source: dict[str, Any] | None = None
    why: dict[str, Any] = Field(default_factory=dict)
    score: float


class SearchStats(BaseModel):
    """召回统计；``channels`` 是各通道过滤后的命中条数。"""

    candidates: int
    returned: int
    latency_ms: float
    mode: str
    channels: dict[str, int] = Field(default_factory=dict)


class SearchResult(BaseModel):
    items: list[SearchItem] = Field(default_factory=list)
    context_text: str = ""
    stats: SearchStats
    degraded: list[str] = Field(default_factory=list)


__all__ = [
    "MAX_QUERY_CHARS",
    "MAX_SEARCH_LIMIT",
    "SearchItem",
    "SearchMode",
    "SearchRequest",
    "SearchResult",
    "SearchStats",
    "TimeRange",
]
