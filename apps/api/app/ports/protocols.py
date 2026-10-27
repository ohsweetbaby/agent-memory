"""外部能力端口：嵌入、LLM 与重排。

应用核心只依赖这些结构化协议，具体实现放在 ``app.adapters``，测试可注入
确定性替身。协议均为 ``async``，适配器内部可以把同步 SDK 放进线程池。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """文本嵌入；``name`` 是模型名，``provider`` 是供应商短名。

    ``identity`` 是嵌入缓存的稳定身份（``provider:model``），切换供应商或
    模型时必须变化，避免把旧向量误当成当前配置的结果。
    """

    name: str
    provider: str
    identity: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """返回与 ``texts`` 等长的向量列表，顺序必须一致。"""
        ...


@runtime_checkable
class LLMProvider(Protocol):
    """OpenAI 兼容的对话补全；``json_schema`` 非空时要求结构化输出。"""

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        ...


@runtime_checkable
class Reranker(Protocol):
    """对候选文档重新打分；分数越大越相关。"""

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        ...


__all__ = ["EmbeddingProvider", "LLMProvider", "Reranker"]
