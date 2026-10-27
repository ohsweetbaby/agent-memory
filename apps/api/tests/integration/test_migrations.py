"""初始迁移的 up/down 与关键结构检查。"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.models import Base

pytestmark = pytest.mark.integration

M1_TABLES = {
    "workspace",
    "agent",
    "end_user",
    "session",
    "memory",
    "memory_embedding",
    "memory_link",
    "entity",
    "memory_entity",
    "episode",
    "review_item",
    "job_run",
    "audit_log",
    "outbox_event",
    "projection_state",
    "api_key",
    "provider_config",
}


def test_initial_migration_up_and_down(
    db_engine: Engine, run_alembic, alembic_config
) -> None:
    run_alembic("upgrade", "head")
    inspector = inspect(db_engine)
    assert M1_TABLES <= set(inspector.get_table_names())

    memory_columns = {column["name"] for column in inspector.get_columns("memory")}
    assert {"version", "search_tsv", "metadata", "valid_from", "valid_to"} <= memory_columns

    # M2/M3 的表在 M1 不应提前创建。
    assert "document" not in inspector.get_table_names()
    assert "asset" not in inspector.get_table_names()
    assert "message" not in inspector.get_table_names()

    with db_engine.connect() as connection:
        assert connection.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        ).scalar_one() == 1
        assert connection.execute(
            text(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'memory_search_tsv_trigger' AND NOT tgisinternal"
            )
        ).scalar_one() == 1

    run_alembic("downgrade", "base")
    assert "memory" not in set(inspect(db_engine).get_table_names())


def test_models_match_migration(db_engine: Engine, run_alembic) -> None:
    """模型与迁移漂移即失败，防止 M2 迁移基于过期元数据生成。"""
    run_alembic("upgrade", "head")
    with db_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []

