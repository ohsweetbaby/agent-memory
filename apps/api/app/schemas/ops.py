"""运维配置的请求/响应模型：模型 Provider、连通性探测与保留策略。

Provider 密钥只允许写入：创建/更新请求里的 ``api_key`` 是 write-only，
任何响应都不包含明文或密文，只暴露 ``has_secret`` 掩码状态。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ProviderKind = Literal["embedding", "llm", "reranker"]

#: 连通性探测的硬超时；前端提示与后端保持一致。
PROVIDER_TEST_TIMEOUT_SECONDS = 5.0


class ProviderCreate(BaseModel):
    """创建 Provider；``api_key`` 仅用于写入加密列。"""

    kind: ProviderKind
    name: str = Field(min_length=1, max_length=255)
    base_url: str | None = Field(default=None, max_length=1024)
    model: str | None = Field(default=None, max_length=255)
    dim: int | None = Field(default=None, ge=1, le=1_000_000)
    extra: dict[str, Any] = Field(default_factory=dict)
    api_key: str | None = Field(default=None, max_length=4096)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("名称不能为空")
        return text

    @field_validator("base_url", "model")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class ProviderUpdate(BaseModel):
    """局部更新 Provider；留空/缺省的 ``api_key`` 表示不修改密钥。"""

    kind: ProviderKind | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, max_length=1024)
    model: str | None = Field(default=None, max_length=255)
    dim: int | None = Field(default=None, ge=1, le=1_000_000)
    extra: dict[str, Any] | None = None
    api_key: str | None = Field(default=None, max_length=4096)
    enabled: bool | None = None
    #: 显式清除已保存的密钥；与 ``api_key`` 互斥。
    clear_secret: bool = False

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("名称不能为空")
        return text


class ProviderTestRequest(BaseModel):
    """连通性探测入参。

    新增 Provider 尚未保存时直接传表单字段；编辑已有 Provider 时传
    ``provider_id``，未重新填写 ``api_key`` 则由后端读取已加密的密钥。
    """

    provider_id: str | None = Field(default=None, max_length=36)
    base_url: str | None = Field(default=None, max_length=1024)
    api_key: str | None = Field(default=None, max_length=4096)


class ProviderTestResult(BaseModel):
    """探测结果；``ok=False`` 时 ``message`` 必须是可读中文原因。"""

    ok: bool
    message: str
    latency_ms: float
    status_code: int | None = None


class ProviderView(BaseModel):
    """Provider 列表项：永不包含 ``api_key``/``encrypted_secret``。"""

    id: str
    kind: str
    name: str
    base_url: str | None = None
    model: str | None = None
    dim: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    has_secret: bool


class OpsPolicy(BaseModel):
    """设置页展示的遗忘/保留参数；全部来自当前应用配置。"""

    decay_archive_threshold: float
    purge_retention_days: int
    review_retention_days: int
    audit_retention_days: int
    consolidation_idle_minutes: int
    consolidation_nightly: str
    search_context_budget_tokens: int
    search_query_rewrite: bool


class ProviderListResponse(BaseModel):
    items: list[ProviderView] = Field(default_factory=list)
    policy: OpsPolicy


__all__ = [
    "PROVIDER_TEST_TIMEOUT_SECONDS",
    "OpsPolicy",
    "ProviderCreate",
    "ProviderKind",
    "ProviderListResponse",
    "ProviderTestRequest",
    "ProviderTestResult",
    "ProviderUpdate",
    "ProviderView",
]
