"""评测/压测共用的真实基础设施栈。

只被测试与评测脚本使用；生产运行不依赖 testcontainers。容器镜像与
``apps/api/tests/integration/conftest.py`` 保持一致，全部使用本地已有 tag，
避免 CI 访问 Docker Hub。
"""

from __future__ import annotations

import os

# Docker Hub 在当前环境不可达；容器由本模块显式 stop。
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

import time  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402
from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import Engine, make_url  # noqa: E402
from testcontainers.core.container import DockerContainer  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

API_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = API_ROOT / "alembic.ini"
ALEMBIC_SCRIPT = API_ROOT / "alembic"

POSTGRES_IMAGE = "postgres:16-alpine"
QDRANT_IMAGE = "qdrant/qdrant:v1.12.4"
NEO4J_IMAGE = "neo4j:5-community"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "test-password"


def published_url(container: DockerContainer, port: int) -> str:
    """testcontainers 在部分驱动下 host 是 localhost；统一走这里。"""
    return f"{container.get_container_host_ip()}:{container.get_exposed_port(port)}"


def wait_for_http(url: str, path: str = "/readyz", timeout: float = 90.0) -> None:
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


@dataclass
class InfraStack:
    """一次 pytest 会话或一次独立评测使用的 Postgres/Qdrant/Neo4j。"""

    postgres: PostgresContainer
    qdrant: DockerContainer
    neo4j: DockerContainer
    _engines: list[Engine] = field(default_factory=list)
    _databases: list[str] = field(default_factory=list)

    @property
    def qdrant_url(self) -> str:
        return f"http://{published_url(self.qdrant, 6333)}"

    @property
    def neo4j_uri(self) -> str:
        return f"bolt://{published_url(self.neo4j, 7687)}"

    @property
    def postgres_url(self) -> str:
        return self.postgres.get_connection_url()

    def new_engine(self, prefix: str) -> Engine:
        """在共享 Postgres 容器里建独立数据库，返回指向它的引擎。"""
        base_url = make_url(self.postgres_url)
        database = f"{prefix}_{uuid4().hex[:10]}"

        admin_engine = create_engine(base_url, isolation_level="AUTOCOMMIT")
        try:
            with admin_engine.connect() as connection:
                connection.execute(text(f'CREATE DATABASE "{database}"'))
        finally:
            admin_engine.dispose()

        engine = create_engine(base_url.set(database=database), pool_pre_ping=True)
        self._engines.append(engine)
        self._databases.append(database)
        return engine

    def migrate(self, engine: Engine) -> None:
        config = Config(str(ALEMBIC_INI))
        config.set_main_option("script_location", str(ALEMBIC_SCRIPT))
        config.set_main_option(
            "sqlalchemy.url", engine.url.render_as_string(hide_password=False)
        )
        command.upgrade(config, "head")

    def close(self) -> None:
        for engine in self._engines:
            engine.dispose()
        self._engines.clear()
        for container in (self.neo4j, self.qdrant, self.postgres):
            try:
                container.stop()
            except Exception:  # 容器可能已自行退出
                pass


def start_stack(*, qdrant: bool = True, neo4j: bool = True) -> InfraStack:
    """启动评测所需容器；调用方负责 ``close()``。"""
    postgres = PostgresContainer(POSTGRES_IMAGE, driver="psycopg")
    postgres.start()

    qdrant_container = DockerContainer(QDRANT_IMAGE).with_exposed_ports(6333)
    qdrant_container.start()
    wait_for_http(f"http://{published_url(qdrant_container, 6333)}", "/readyz")

    neo4j_container = (
        DockerContainer(NEO4J_IMAGE)
        .with_env("NEO4J_AUTH", f"{NEO4J_USER}/{NEO4J_PASSWORD}")
        .with_exposed_ports(7687)
    )
    neo4j_container.start()

    return InfraStack(
        postgres=postgres,
        qdrant=qdrant_container,
        neo4j=neo4j_container,
    )


__all__ = [
    "InfraStack",
    "NEO4J_PASSWORD",
    "NEO4J_USER",
    "start_stack",
    "published_url",
    "wait_for_http",
]
