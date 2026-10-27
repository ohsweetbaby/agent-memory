"""LLM Provider 与重排器。

M1 只接 OpenAI 兼容的对话补全；重排默认 :class:`NoopReranker`，保持召回
原顺序，真实 cross-encoder 留到后续里程碑。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class OpenAICompatibleLLM:
    """``POST {base_url}/chat/completions`` 的最小封装。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.transport = transport

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": json_schema,
                    "strict": True,
                },
            }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport
        ) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()

        choices = response.json().get("choices") or []
        if not choices:
            raise ValueError("LLM 响应缺少 choices")
        return str(choices[0].get("message", {}).get("content") or "")


class NoopReranker:
    """不改变召回顺序的重排器；所有文档同分。"""

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        return [0.0 for _ in documents]


def build_llm_provider(settings: Settings) -> OpenAICompatibleLLM | None:
    """未配置 API Key 时不创建远端 LLM，调用方按 ``None`` 降级。"""
    if not settings.llm_api_key:
        return None
    return OpenAICompatibleLLM(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
    )


def build_reranker(settings: Settings) -> NoopReranker:
    """M1 固定使用 Noop；保留工厂签名便于后续替换真实模型。"""
    return NoopReranker()


__all__ = [
    "NoopReranker",
    "OpenAICompatibleLLM",
    "build_llm_provider",
    "build_reranker",
]
