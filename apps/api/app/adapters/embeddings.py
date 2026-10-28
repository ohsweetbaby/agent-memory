"""嵌入 Provider 实现与工厂。

- :class:`FakeEmbedding`：确定性、无依赖，供测试与本地开发。
- :class:`OpenAICompatibleEmbedding`：任意 OpenAI 兼容的 ``/embeddings``。
- :class:`LocalSentenceTransformerEmbedding`：懒加载可选依赖，未安装时只有
  真正调用才报清晰的安装提示。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import threading
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

_LOCAL_PROVIDERS = {"local", "sentence-transformers", "sentence_transformers", "st"}
_FAKE_PROVIDERS = {"fake", "stub", "test", "deterministic"}

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_WORD_FEATURE = re.compile(r"[a-z0-9_]+")


def _normalize(values: list[float]) -> list[float]:
    """L2 归一化；零向量保持为零，避免除零。"""
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        return values
    return [value / norm for value in values]


class FakeEmbedding:
    """确定性词面哈希嵌入：共享词/汉字的文本余弦更高。

    早期实现用整段文本哈希成随机向量，只能验证“确定性”，无法支撑黄金集
    的检索质量基线。这里改为可复现的 feature hashing（中文二元组 + 单字，
    英文/数字整词），不依赖模型或网络，同一文本永远得到同一向量，且相似
    文本不会因哈希随机性而互相淹没。它仍只用于测试与本地开发。
    """

    provider = "fake"

    def __init__(self, dim: int = 8, model: str | None = None) -> None:
        if dim <= 0:
            raise ValueError("dim 必须为正数")
        self.dim = dim
        # 即使调用方传入真实模型名，fake 也必须保留独立身份，避免假向量
        # 进入真实模型的缓存；仅在显式提供 fake 名称时沿用。
        if model and model.startswith("fake"):
            self.name = model
        else:
            self.name = f"fake-{dim}"

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.name}"

    def _features(self, text: str) -> list[tuple[str, float]]:
        """提取可哈希特征；CJK 用二元组，单字作弱特征，英文保留整词。"""
        lowered = text.casefold()
        features: list[tuple[str, float]] = []
        for run in _CJK_RUN.findall(lowered):
            if len(run) == 1:
                features.append((run, 1.0))
                continue
            features.extend((run[index : index + 2], 1.0) for index in range(len(run) - 1))
            features.extend((char, 0.35) for char in run)
        features.extend((word, 1.0) for word in _WORD_FEATURE.findall(lowered))
        return features

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        features = self._features(text)
        if not features:
            # 标点/空白等无特征文本仍需要稳定且互不相同的向量。
            digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
            features = [(digest.hex(), 1.0)]
        for feature, weight in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = value % self.dim
            sign = 1.0 if (value >> 63) & 1 else -1.0
            vector[index] += sign * weight
        # 极端哈希碰撞导致零向量时退回文本哈希，保证单位长度约定。
        if not any(vector):
            digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = value % self.dim
            vector[index] = 1.0 if value & 1 else -1.0
        return _normalize(vector)

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        """同步入口；供脚本与测试直接比较确定性。"""
        return [self._vector(text) for text in texts]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return self.embed_sync(texts)


class OpenAICompatibleEmbedding:
    """调用 OpenAI 兼容的 ``POST {base_url}/embeddings``。"""

    provider = "openai"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dim: int,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if dim <= 0:
            raise ValueError("dim 必须为正数")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.name = model
        self.dim = dim
        self.timeout = timeout
        self.transport = transport

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.name}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport
        ) as client:
            response = await client.post(
                f"{self.base_url}/embeddings",
                json={"model": self.name, "input": texts},
                headers=headers,
            )
            response.raise_for_status()

        data = response.json().get("data") or []
        if len(data) != len(texts):
            raise ValueError(
                f"嵌入响应数量不匹配: 期望 {len(texts)} 条，实际 {len(data)} 条"
            )

        vectors: list[list[float] | None] = [None] * len(texts)
        for item in data:
            index = int(item.get("index", 0))
            vector = item.get("embedding")
            if not isinstance(vector, list) or len(vector) != self.dim:
                actual = len(vector) if isinstance(vector, list) else "未知"
                raise ValueError(
                    f"嵌入维度不匹配: 期望 {self.dim}，实际 {actual}"
                )
            if not 0 <= index < len(vectors):
                raise ValueError(f"嵌入响应 index 越界: {index}")
            vectors[index] = [float(value) for value in vector]

        if any(vector is None for vector in vectors):
            raise ValueError("嵌入响应缺少部分 index")
        return [vector for vector in vectors if vector is not None]


class LocalSentenceTransformerEmbedding:
    """基于 ``sentence-transformers`` 的本地嵌入。

    模块导入不触发重依赖加载；模型在首次调用时加载，未安装可选依赖时抛出
    带安装命令的 :class:`RuntimeError`。
    """

    provider = "local"

    def __init__(self, model: str, dim: int) -> None:
        if dim <= 0:
            raise ValueError("dim 必须为正数")
        self.name = model
        self.dim = dim
        self._model: Any | None = None
        self._lock = threading.Lock()

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.name}"

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:  # pragma: no cover - 并发双检
                return self._model
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # 可选依赖缺失
                raise RuntimeError(
                    "本地嵌入需要可选依赖 sentence-transformers；"
                    "请执行 `uv sync --extra local-embeddings` "
                    "或 `pip install 'agent-memory-api[local-embeddings]'`"
                ) from exc
            self._model = SentenceTransformer(self.name)
        return self._model

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        encoded = model.encode(texts, normalize_embeddings=True)
        return [[float(value) for value in vector] for vector in encoded]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._embed_sync, texts)


def build_embedding_provider(settings: Settings) -> Any:
    """按配置选择嵌入 Provider；未知名称回落 OpenAI 兼容实现。"""
    provider = settings.embedding_provider.strip().lower()
    if provider in _FAKE_PROVIDERS:
        # Fake 永远使用 fake 专属名称；即使 EMBEDDING_MODEL 配成真实模型名，
        # 也不会冒用该模型的身份写入缓存。
        return FakeEmbedding(dim=settings.embedding_dim)
    if provider in _LOCAL_PROVIDERS:
        return LocalSentenceTransformerEmbedding(
            settings.embedding_model, dim=settings.embedding_dim
        )
    return OpenAICompatibleEmbedding(
        base_url=settings.embedding_base_url or settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
    )


__all__ = [
    "FakeEmbedding",
    "LocalSentenceTransformerEmbedding",
    "OpenAICompatibleEmbedding",
    "build_embedding_provider",
]
