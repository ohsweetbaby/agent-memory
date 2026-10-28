"""Worker 运行时容器。

Celery worker 与 CLI 共用同一份配置：数据库会话、嵌入 Provider、Qdrant 与
Neo4j 索引。测试通过 :func:`configure_runtime` 注入当前事务与替身；生产环境
由 :func:`build_runtime` 按 ``Settings`` 惰性构建。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ContextManager

from sqlalchemy.orm import Session

from app.adapters.embeddings import build_embedding_provider
from app.adapters.llm import build_llm_provider
from app.adapters.neo4j import Neo4jIndex
from app.adapters.qdrant import QdrantIndex
from app.config import Settings, get_settings
from app.db import SessionLocal

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Runtime:
    """一次 worker/CLI 执行所需的依赖。"""

    session_scope: Callable[[], ContextManager[Session]]
    embedding: Any
    qdrant: Any
    neo4j: Any | None = None
    eager: bool = False
    #: 对话抽取使用的 LLM；未配置 Key 时为 None，抽取任务会进入死信。
    llm: Any | None = None

    def close(self) -> None:
        for resource in (self.qdrant, self.neo4j):
            close = getattr(resource, "close", None)
            if callable(close):
                close()


_runtime: Runtime | None = None


def configure_runtime(runtime: Runtime) -> None:
    """测试或嵌入式调用注入运行时。"""
    global _runtime
    _runtime = runtime


def reset_runtime() -> None:
    """清除注入的运行时；不关闭资源（由调用方/ fixture 负责）。"""
    global _runtime
    _runtime = None


@contextmanager
def _session_scope() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def build_runtime(settings: Settings | None = None) -> Runtime:
    resolved = settings or get_settings()
    embedding = build_embedding_provider(resolved)
    qdrant = QdrantIndex(
        resolved.qdrant_url,
        provider=embedding.provider,
        model=embedding.name,
        dim=embedding.dim,
    )
    # compose.lite 用空 NEO4J_URI 表示“无图库”；此时图通道应降级而不是
    # 让整个 runtime 构造失败。
    neo4j = (
        Neo4jIndex.from_settings(resolved)
        if resolved.neo4j_uri.strip()
        else None
    )
    return Runtime(
        session_scope=_session_scope,
        embedding=embedding,
        qdrant=qdrant,
        neo4j=neo4j,
        llm=build_llm_provider(resolved),
    )


def peek_runtime() -> Runtime | None:
    """返回已注入的运行时；不会触发外部客户端构建。

    API 进程在硬删除时只做“已配置运行时”的同步级联，生产环境仍由 outbox
    worker 兜底；未配置时返回 ``None``，避免请求线程连接外部服务。
    """
    return _runtime


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = build_runtime()
        logger.info("worker runtime initialized")
    return _runtime


__all__ = [
    "Runtime",
    "build_runtime",
    "configure_runtime",
    "get_runtime",
    "peek_runtime",
    "reset_runtime",
]
