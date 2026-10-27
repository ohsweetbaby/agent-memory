"""集成测试基础设施：真实 PostgreSQL + Redis + Alembic 迁移。

每个测试使用独立的数据库，避免迁移 up/down 与数据回滚互相干扰。
Docker 不可用时应先运行单元测试（``apps/api/tests/unit`` 与
``packages/memory-core/tests``），本目录的测试按设计依赖 Docker。
"""

from __future__ import annotations

import os

# Docker Hub 在当前环境不可达；Ryuk 镜像无法拉取。容器由 fixture 显式 stop，
# 因此关闭 Ryuk 只影响进程崩溃时的兜底清理。
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

import time  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Any  # noqa: E402
from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import redis  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.engine import Engine, make_url  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from testcontainers.community.redis import RedisContainer  # noqa: E402
from testcontainers.core.container import DockerContainer  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from app.adapters.neo4j import Neo4jIndex  # noqa: E402
from app.adapters.qdrant import QdrantIndex  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import build_session_factory, get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import AdminUser  # noqa: E402

API_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = API_ROOT / "alembic.ini"
ALEMBIC_SCRIPT = API_ROOT / "alembic"

# 使用本地已有 tag，避免 CI/离线环境访问 Docker Hub 失败。
POSTGRES_IMAGE = "postgres:16-alpine"
REDIS_IMAGE = "redis:7-alpine"
QDRANT_IMAGE = "qdrant/qdrant:v1.12.4"
NEO4J_IMAGE = "neo4j:5-community"
NEO4J_PASSWORD = "test-password"

# 集成测试专用密钥；crypto/会话签名都从当前应用配置读取。
TEST_SECRET_KEY = "integration-test-secret-key"
ADMIN_PASSWORD = "p" * 12


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """整个测试会话共享一个 Postgres 容器，测试间用独立数据库隔离。"""
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        yield container


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    """登录限流与 API Key 限流必须走真实 Redis 路径。"""
    with RedisContainer(REDIS_IMAGE) as container:
        yield container


@pytest.fixture(scope="session")
def redis_url(redis_container: RedisContainer) -> str:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


def _published_url(container: DockerContainer, port: int) -> str:
    """testcontainers 在部分驱动的 host 是 ``localhost``；URL 统一走这里。"""
    host = container.get_container_host_ip()
    published = container.get_exposed_port(port)
    return f"{host}:{published}"


def _wait_for_http(url: str, path: str = "/readyz", timeout: float = 90.0) -> None:
    """轮询容器 HTTP 就绪端点；端口开放不代表服务已可用。"""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{url}{path}", timeout=2.0)
            if response.status_code < 500:
                return
        except Exception as exc:  # 容器刚启动时连接会被拒绝
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"服务未在 {timeout:.0f}s 内就绪: {url}{path}") from last_error


@pytest.fixture(scope="session")
def qdrant_container() -> Iterator[DockerContainer]:
    """Qdrant 集成测试共享一个容器；按测试使用独立 collection。"""
    with DockerContainer(QDRANT_IMAGE).with_exposed_ports(6333) as container:
        _wait_for_http(f"http://{_published_url(container, 6333)}", "/readyz")
        yield container


@pytest.fixture(scope="session")
def qdrant_url(qdrant_container: DockerContainer) -> str:
    return f"http://{_published_url(qdrant_container, 6333)}"


@pytest.fixture()
def qdrant_index(qdrant_url: str) -> Iterator[QdrantIndex]:
    """指向测试容器的 FakeEmbedding 维度 collection，测试后清理。"""
    index = QdrantIndex(qdrant_url, provider="fake", model="fake-8", dim=8)
    index.recreate_collection()
    try:
        yield index
    finally:
        index.delete_collection()
        index.close()


@pytest.fixture(scope="session")
def neo4j_container() -> Iterator[DockerContainer]:
    """Neo4j Community 启动较慢，会话级共享；就绪由驱动轮询。"""
    container = (
        DockerContainer(NEO4J_IMAGE)
        .with_env("NEO4J_AUTH", f"neo4j/{NEO4J_PASSWORD}")
        .with_exposed_ports(7687)
    )
    with container:
        yield container


@pytest.fixture()
def neo4j_index(neo4j_container: DockerContainer) -> Iterator[Neo4jIndex]:
    """指向测试容器的图索引；每个测试前清空 M1 节点，避免残留。"""
    index = Neo4jIndex(
        f"bolt://{_published_url(neo4j_container, 7687)}",
        "neo4j",
        NEO4J_PASSWORD,
    )
    try:
        index.wait_ready(timeout=180.0)
        index.clear()
        yield index
    finally:
        index.clear()
        index.close()



@pytest.fixture(autouse=True)
def clean_redis(redis_url: str) -> Iterator[None]:
    """每个测试从空 Redis 开始，避免限流计数跨测试泄漏。"""
    client = redis.Redis.from_url(redis_url)
    client.flushdb()
    try:
        yield
    finally:
        client.flushdb()
        client.close()


@pytest.fixture()
def db_engine(postgres_container: PostgresContainer) -> Iterator[Engine]:
    """为单个测试创建并销毁独立数据库。"""
    base_url = make_url(postgres_container.get_connection_url())
    database = f"test_{uuid4().hex[:12]}"

    admin_engine = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database}"'))

    engine = create_engine(
        base_url.set(database=database),
        pool_pre_ping=True,
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.fixture()
def alembic_config(db_engine: Engine) -> Config:
    """绑定到当前测试数据库的 Alembic 配置。"""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT))
    config.set_main_option(
        "sqlalchemy.url", db_engine.url.render_as_string(hide_password=False)
    )
    return config


@pytest.fixture()
def run_alembic(alembic_config: Config) -> Callable[[str, str], None]:
    """执行 ``upgrade`` / ``downgrade`` 的小工具。"""

    def _run(action: str, revision: str) -> None:
        if action == "upgrade":
            command.upgrade(alembic_config, revision)
        elif action == "downgrade":
            command.downgrade(alembic_config, revision)
        elif action == "stamp":
            command.stamp(alembic_config, revision)
        else:  # pragma: no cover - 防御性分支
            raise ValueError(f"unsupported alembic action: {action}")

    return _run


@pytest.fixture()
def migrated_engine(db_engine: Engine, run_alembic: Callable[[str, str], None]) -> Engine:
    run_alembic("upgrade", "head")
    return db_engine


@pytest.fixture()
def db_session(migrated_engine: Engine) -> Iterator[Session]:
    """外层事务包裹整个测试，结束后回滚，保持数据库干净。"""
    connection = migrated_engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def session_factory(migrated_engine: Engine) -> sessionmaker[Session]:
    """会真实提交的会话工厂，供 API 与 fixture 写入共享数据。"""
    return build_session_factory(migrated_engine)


@pytest.fixture()
def app_factory(migrated_engine: Engine, redis_url: str) -> Callable[..., FastAPI]:
    """构造指向本测试容器、可覆盖配置的应用实例。"""
    factory = build_session_factory(migrated_engine)

    def _build(**overrides: Any) -> FastAPI:
        options: dict[str, Any] = {
            "environment": "test",
            "database_url": migrated_engine.url.render_as_string(hide_password=False),
            "redis_url": redis_url,
            "secret_key": TEST_SECRET_KEY,
        }
        options.update(overrides)
        application = create_app(Settings(**options))

        def _get_db() -> Iterator[Session]:
            session = factory()
            try:
                yield session
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        application.dependency_overrides[get_db] = _get_db
        return application

    return _build


@pytest.fixture()
def app(app_factory: Callable[..., FastAPI]) -> Iterator[FastAPI]:
    """把应用的 ``get_db`` / Redis 指向本测试的容器。"""
    application = app_factory()
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture()
def admin(client, session_factory: sessionmaker[Session]) -> AdminUser:
    """创建并登录管理员；后续写请求自动带 CSRF 头。"""
    created = client.post("/api/v1/auth/setup", json={"password": ADMIN_PASSWORD})
    assert created.status_code == 201, created.text

    logged_in = client.post("/api/v1/auth/login", json={"password": ADMIN_PASSWORD})
    assert logged_in.status_code == 200, logged_in.text
    client.headers["X-CSRF-Token"] = logged_in.json()["csrf_token"]

    with session_factory() as session:
        return session.scalars(select(AdminUser)).one()


@pytest.fixture()
def workspace(client, admin: AdminUser) -> dict[str, Any]:
    response = client.post("/api/v1/workspaces", json={"name": "测试工作区"})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def read_key(client, workspace: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/api/v1/keys",
        json={"workspace_id": workspace["id"], "name": "cursor", "scopes": ["read"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def write_key(client, workspace: dict[str, Any]) -> str:
    """仅有 write（无 admin）的 API Key，用于作用域边界测试。"""
    response = client.post(
        "/api/v1/keys",
        json={"workspace_id": workspace["id"], "name": "writer", "scopes": ["write"]},
    )
    assert response.status_code == 201, response.text
    return response.json()["key"]


@pytest.fixture()
def valid_write_payload(workspace: dict[str, Any]) -> Callable[..., dict[str, Any]]:
    """构造合法写入载荷；用例只覆盖需要变化的字段。"""

    def _make(**overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "workspace_id": workspace["id"],
            "content": "默认记忆内容",
            "type": "semantic",
            "scope": "user",
            "scope_id": "user-1",
        }
        payload.update(overrides)
        return payload

    return _make


@pytest.fixture()
def memory(client, admin, valid_write_payload: Callable[..., dict[str, Any]]):
    """通过公开 API 创建一条记忆，返回带 ``id`` 的轻量对象。"""
    response = client.post(
        "/api/v1/memories", json=valid_write_payload(content="用户喜欢喝绿茶")
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return SimpleNamespace(**body)


@pytest.fixture()
def revoked_key(client, read_key: dict[str, Any]) -> str:
    response = client.delete(f"/api/v1/keys/{read_key['id']}")
    assert response.status_code == 204, response.text
    return read_key["key"]
