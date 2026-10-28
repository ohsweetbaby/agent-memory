"""API 测试共享 fixture。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app

# 评测/压测共享 support 包；子目录 conftest 也会复用这里的 stack fixture。
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from support.stack import InfraStack, start_stack  # noqa: E402


@pytest.fixture(scope="session")
def stack() -> Iterator[InfraStack]:
    """真实 Postgres + Qdrant + Neo4j；仅 eval/perf 测试会请求它。"""
    infra = start_stack()
    try:
        yield infra
    finally:
        infra.close()


@pytest.fixture()
def app() -> Iterator[FastAPI]:
    """每个测试使用独立的应用实例，避免状态串联。"""
    application = create_app()
    yield application
    application.dependency_overrides.clear()


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    """同步测试客户端。"""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def override_dependency(app: FastAPI) -> Callable[[Any, Any], None]:
    """依赖覆盖入口：override_dependency(原依赖, 替身)。"""

    def _override(dependency: Any, value: Any) -> None:
        app.dependency_overrides[dependency] = lambda: value

    return _override
