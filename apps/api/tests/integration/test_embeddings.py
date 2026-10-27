"""嵌入 Provider：确定性、维度、HTTP 兼容与可选依赖边界。

这些用例不依赖 Docker；放在 integration 目录是为了与任务简报中的测试路径一致。
"""

from __future__ import annotations

import asyncio
import builtins
import math

import httpx
import pytest

from app.adapters.embeddings import (
    FakeEmbedding,
    LocalSentenceTransformerEmbedding,
    OpenAICompatibleEmbedding,
    build_embedding_provider,
)
from app.adapters.qdrant import collection_name
from app.config import Settings


def test_fake_embedding_is_deterministic() -> None:
    p = FakeEmbedding(dim=8)
    assert p.embed_sync(["相同文本"]) == p.embed_sync(["相同文本"])


def test_fake_embedding_is_async_and_unit_length() -> None:
    p = FakeEmbedding(dim=8)
    vectors = asyncio.run(p.embed(["第一段", "第二段"]))
    assert len(vectors) == 2
    assert all(len(vector) == 8 for vector in vectors)
    assert vectors[0] != vectors[1]
    assert math.isclose(sum(value * value for value in vectors[0]), 1.0, rel_tol=1e-6)


def test_openai_compatible_embedding_parses_response_by_index() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["payload"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    provider = OpenAICompatibleEmbedding(
        base_url="https://example.test/v1",
        api_key="secret-key",
        model="embed-small",
        dim=2,
        transport=httpx.MockTransport(handler),
    )
    vectors = asyncio.run(provider.embed(["a", "b"]))
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert str(captured["url"]).endswith("/embeddings")
    assert captured["authorization"] == "Bearer secret-key"
    assert '"input"' in str(captured["payload"])


def test_openai_compatible_embedding_rejects_wrong_dimension() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    provider = OpenAICompatibleEmbedding(
        base_url="https://example.test/v1",
        api_key="",
        model="embed-small",
        dim=3,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ValueError, match="维度"):
        asyncio.run(provider.embed(["a"]))


def test_local_embedding_reports_missing_optional_dependency(monkeypatch) -> None:
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "sentence_transformers" or name.startswith("sentence_transformers."):
            raise ModuleNotFoundError("No module named 'sentence_transformers'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    provider = LocalSentenceTransformerEmbedding("all-MiniLM-L6-v2", dim=384)
    with pytest.raises(RuntimeError, match="sentence-transformers"):
        asyncio.run(provider.embed(["hello"]))


def test_build_embedding_provider_selects_fake_and_collection_name() -> None:
    provider = build_embedding_provider(
        Settings(
            embedding_provider="fake",
            embedding_model="fake-8",
            embedding_dim=8,
        )
    )
    assert isinstance(provider, FakeEmbedding)
    assert provider.name == "fake-8"
    assert provider.dim == 8
    assert (
        collection_name(provider.provider, provider.name, provider.dim)
        == "mem_fake_fake-8_8"
    )
    assert (
        collection_name("openai", "text-embedding-3-small", 1536)
        == "mem_openai_text-embedding-3-small_1536"
    )
