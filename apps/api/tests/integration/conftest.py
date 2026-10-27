"""集成测试基础设施：真实 PostgreSQL + Alembic 迁移。

每个测试使用独立的数据库，避免迁移 up/down 与数据回滚互相干扰。
Docker 不可用时应先运行单元测试（``apps/api/tests/unit`` 与
``packages/memory-core/tests``），本目录的测试按设计依赖 Docker。
"""

from __future__ import annotations

import os

# Docker Hub 在当前环境不可达；Ryuk 镜像无法拉取。容器由 fixture 显式 stop，
# 因此关闭 Ryuk 只影响进程崩溃时的兜底清理。
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

from collections.abc import Callable, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import Engine, make_url  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

API_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = API_ROOT / "alembic.ini"
ALEMBIC_SCRIPT = API_ROOT / "alembic"

# 使用本地已有 tag，避免 CI/离线环境访问 Docker Hub 失败。
POSTGRES_IMAGE = "postgres:16-alpine"


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """整个测试会话共享一个 Postgres 容器，测试间用独立数据库隔离。"""
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        yield container


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
