"""MCP 契约测试夹具。

测试使用官方 MCP Python SDK：服务端挂载在 FastAPI 的 ``/mcp``，客户端通过
``httpx.ASGITransport`` 直连 ASGI 应用，不额外监听端口；这样既能走到真实的
Streamable HTTP 编解码，也能复用集成测试的 Postgres / Redis 容器。

检索所需的向量与图服务用替身：``qdrant=None`` / ``neo4j=None`` 时检索服务
按规格 §7.5 降级到全文 + 先验，契约测试无需再启动两个容器。
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Any

# contract 与 integration 同属 ``apps/api/tests``（无 __init__.py），把该目录
# 放进 sys.path 后即可复用集成测试的容器 fixture，避免复制一份基础设施。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import pytest  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from integration.conftest import *  # noqa: E402,F401,F403
from integration.conftest import TEST_SECRET_KEY  # noqa: E402

from app.adapters.embeddings import FakeEmbedding  # noqa: E402
from app.auth.api_key import create_api_key  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import get_db  # noqa: E402
from app.jobs.runtime import Runtime, configure_runtime, reset_runtime  # noqa: E402
from app.main import create_app  # noqa: E402
from app.mcp.server import McpServices  # noqa: E402
from app.models import Workspace  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """只跑 asyncio，不重复跑 trio。"""
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_pending_jobs() -> Iterator[None]:
    """抽取/巩固在 eager 运行时只入内存队列，测试间必须清空。"""
    from app.jobs.tasks import reset_pending

    reset_pending()
    try:
        yield
    finally:
        reset_pending()


@pytest.fixture()
def mcp_runtime(session_factory: Any) -> Iterator[Runtime]:
    """检索替身：全文通道真实可用，向量/图通道降级。

    同时注册为进程运行时，让 ``conversation_ingest`` 走 eager 内存队列，
    而不是尝试连接测试环境之外的 Celery broker。
    """
    runtime = Runtime(
        session_scope=lambda: nullcontext(session_factory()),
        embedding=FakeEmbedding(dim=8),
        qdrant=None,
        neo4j=None,
        eager=True,
    )
    configure_runtime(runtime)
    try:
        yield runtime
    finally:
        reset_runtime()


@pytest.fixture()
def mcp_app(
    migrated_engine: Any,
    redis_url: str,
    session_factory: Any,
    mcp_runtime: Runtime,
) -> Iterator[Any]:
    """与生产一致的 FastAPI 应用，但数据库/Redis 指向测试容器。"""
    settings = Settings(
        environment="test",
        database_url=migrated_engine.url.render_as_string(hide_password=False),
        redis_url=redis_url,
        secret_key=TEST_SECRET_KEY,
    )
    application = create_app(
        settings,
        mcp_services=McpServices(
            session_factory=session_factory,
            runtime_provider=lambda: mcp_runtime,
        ),
    )

    def _get_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    application.dependency_overrides[get_db] = _get_db
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture()
def mcp_workspace(session_factory: Any) -> str:
    """固定 id 的 workspace；简报用例直接使用 ``ws1``。"""
    with session_factory() as session:
        session.add(Workspace(id="ws1", name="MCP 契约测试"))
        session.commit()
    return "ws1"


@pytest.fixture()
def admin_key(session_factory: Any, mcp_workspace: str) -> str:
    with session_factory() as session:
        _, raw = create_api_key(session, mcp_workspace, "mcp-admin", ["admin"])
        session.commit()
    return raw


@pytest.fixture()
def read_key(session_factory: Any, mcp_workspace: str) -> str:
    with session_factory() as session:
        _, raw = create_api_key(session, mcp_workspace, "mcp-read", ["read"])
        session.commit()
    return raw


@pytest.fixture()
def write_key(session_factory: Any, mcp_workspace: str) -> str:
    with session_factory() as session:
        _, raw = create_api_key(session, mcp_workspace, "mcp-write", ["write"])
        session.commit()
    return raw


@asynccontextmanager
async def open_mcp_client(app: Any, api_key: str) -> AsyncIterator[ClientSession]:
    """启动应用 lifespan（MCP session manager 依赖它）并完成 initialize。"""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", headers=headers
        ) as http_client:
            async with streamable_http_client(
                "http://testserver/mcp", http_client=http_client
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


@pytest.fixture()
async def mcp_client(mcp_app: Any, admin_key: str) -> AsyncIterator[ClientSession]:
    async with open_mcp_client(mcp_app, admin_key) as session:
        yield session


@pytest.fixture()
async def mcp_client_bad_key(mcp_app: Any) -> AsyncIterator[ClientSession]:
    async with open_mcp_client(mcp_app, "mem_notarealkey_0000") as session:
        yield session


@pytest.fixture()
async def mcp_client_read_key(mcp_app: Any, read_key: str) -> AsyncIterator[ClientSession]:
    async with open_mcp_client(mcp_app, read_key) as session:
        yield session


@pytest.fixture()
async def mcp_client_write_key(
    mcp_app: Any, write_key: str
) -> AsyncIterator[ClientSession]:
    async with open_mcp_client(mcp_app, write_key) as session:
        yield session


@pytest.fixture()
async def mcp_client_limited(
    mcp_app: Any, session_factory: Any, mcp_workspace: str
) -> AsyncIterator[ClientSession]:
    """Key 级上限 31：31 次写入通过，第 32 次命中 -32004。

    产品默认写限流是 30 次/分钟；这里显式抬高 1 次，用来覆盖简报中
    “先写 31 次、下一次被限流”的窗口边界，而不改变默认配置。
    """
    with session_factory() as session:
        _, raw = create_api_key(
            session, mcp_workspace, "mcp-limited", ["admin"], rate_limit=31
        )
        session.commit()
    async with open_mcp_client(mcp_app, raw) as session:
        yield session
