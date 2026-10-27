"""应用配置。

所有运行期配置均来自环境变量（或本地 `.env`），仓库内不保存真实密钥。
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量加载的服务配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 运行环境：development 时放开 CORS，生产环境由反向代理同源访问。
    environment: str = "development"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173,http://localhost:8080"

    # 存储与队列
    database_url: str = (
        "postgresql+psycopg://agent_memory:agent_memory@localhost:5432/agent_memory"
    )
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = Field(default="change-me", repr=False)
    secret_key: str = Field(default="dev-only-change-me", repr=False)
    assets_dir: Path = Path("./assets")

    # 认证：会话 Cookie、登录限流与 API Key 限流
    session_cookie_name: str = "am_session"
    session_max_age_seconds: int = 7 * 24 * 3600
    login_failure_limit: int = 5
    login_failure_window_seconds: int = 60
    login_lockout_seconds: int = 900
    api_key_read_rpm: int = 120
    api_key_write_rpm: int = 30

    # MCP：默认启用；未安装官方 SDK 时 /mcp 返回 503，REST 仍可正常启动。
    mcp_enabled: bool = True
    # 反向代理部署时 Host/Origin 由 Nginx 转发，默认不做 DNS rebinding 校验；
    # 直接暴露时可配置逗号分隔的白名单（如 ``mcp.example.com``）重新开启。
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""

    # 模型服务
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    # 嵌入服务可与对话服务不同；留空复用 LLM 的 base_url。
    embedding_base_url: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = Field(default="", repr=False)

    # 对话抽取：达到双阈值才自动入库，否则进入待审队列。
    auto_accept_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    auto_accept_importance: float = Field(default=0.5, ge=0.0, le=1.0)
    review_retention_days: int = Field(default=90, ge=1)
    # 巩固：空闲触发阈值与夜间批量执行时刻（UTC）。
    consolidation_idle_minutes: int = Field(default=30, ge=1)
    consolidation_nightly_hour: int = Field(default=3, ge=0, le=23)
    consolidation_nightly_minute: int = Field(default=0, ge=0, le=59)

    # 遗忘与清理（规格 §5.4/§11/§12）。
    decay_base_stability_days: float = Field(default=7.0, gt=0)
    decay_archive_threshold: float = Field(default=0.15, gt=0, lt=1)
    decay_sweep_hour: int = Field(default=4, ge=0, le=23)
    purge_retention_days: int = Field(default=30, ge=1)
    purge_deleted_hour: int = Field(default=5, ge=0, le=23)
    audit_retention_days: int = Field(default=180, ge=1)
    reconcile_sample_size: int = Field(default=100, ge=1)
    # 导出落到临时目录，生产可用 EXPORT_DIR 指向持久卷。
    export_dir: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "agent-memory-exports"
    )

    # 检索：M1 默认关闭查询改写；上下文预算与规格 §6.3 保持一致。
    search_query_rewrite: bool = False
    search_context_budget_tokens: int = Field(default=1500, ge=0)

    @property
    def is_development(self) -> bool:
        """开发环境才允许跨域访问。"""
        return self.environment.strip().lower() in {"development", "dev", "local"}

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的 CORS 来源解析为列表。"""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        """生产环境强制 Secure Cookie。"""
        return self.environment.strip().lower() in {"production", "prod"}


_current_settings: Settings | None = None


def set_current_settings(settings: Settings) -> None:
    """由 ``create_app`` 注入本次应用实例的配置。"""
    global _current_settings
    _current_settings = settings


def get_current_settings() -> Settings:
    """返回当前应用实例的配置；未创建应用时回退到进程配置。"""
    if _current_settings is not None:
        return _current_settings
    return get_settings()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内复用同一份配置。"""
    return Settings()
