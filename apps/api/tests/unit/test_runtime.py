"""运行时在无图库配置下必须可构造，图通道由检索层降级。"""

from __future__ import annotations

from app.config import Settings
from app.jobs.runtime import build_runtime


def test_build_runtime_allows_empty_neo4j_uri() -> None:
    settings = Settings(
        neo4j_uri="",
        embedding_provider="fake",
        embedding_model="fake-8",
        embedding_dim=8,
    )
    runtime = build_runtime(settings)
    try:
        assert runtime.neo4j is None
        assert runtime.qdrant is not None
    finally:
        runtime.close()
