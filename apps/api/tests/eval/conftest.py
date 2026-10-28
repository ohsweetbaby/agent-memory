"""黄金集评测 fixture：复用根目录 conftest 的真实容器栈。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from app.adapters.embeddings import FakeEmbedding  # noqa: E402
from app.adapters.neo4j import Neo4jIndex  # noqa: E402
from app.adapters.qdrant import QdrantIndex  # noqa: E402
from support.stack import NEO4J_PASSWORD  # noqa: E402


@pytest.fixture(scope="session")
def golden_env(stack):
    """独立数据库 + 当前 collection 的 Qdrant + 真实 Neo4j。"""
    engine = stack.new_engine("golden")
    stack.migrate(engine)
    qdrant = QdrantIndex(
        stack.qdrant_url, provider="fake", model="fake-64", dim=64
    )
    neo4j = Neo4jIndex(stack.neo4j_uri, "neo4j", NEO4J_PASSWORD)
    neo4j.wait_ready(timeout=180.0)

    env = SimpleNamespace(
        engine=engine,
        embedding=FakeEmbedding(dim=64),
        qdrant=qdrant,
        neo4j=neo4j,
    )
    try:
        yield env
    finally:
        try:
            qdrant.delete_collection()
        finally:
            qdrant.close()
        neo4j.clear("ws_golden")
        neo4j.close()
        engine.dispose()
