"""M1 初始 schema：Postgres 事实源。

包含规格 §10.1 的全部 M1 表、索引、pg_trgm 扩展、中文全文检索触发器，
以及 ``memory.version`` 的自增触发器。M2/M3 的 document/message/asset
不在本迁移范围内。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = sa.String(length=36)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TSVECTOR = postgresql.TSVECTOR()


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    # ---- 扩展：模糊匹配必需；中文分词优先 zhparser，缺失时触发器回退 simple ----
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_available_extensions WHERE name = 'zhparser'
            ) THEN
                CREATE EXTENSION IF NOT EXISTS zhparser;
                IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'zh') THEN
                    CREATE TEXT SEARCH CONFIGURATION zh (PARSER = zhparser);
                    ALTER TEXT SEARCH CONFIGURATION zh
                        ADD MAPPING FOR n,v,a,i,e,l WITH simple;
                END IF;
            END IF;
        END $$;
        """
    )

    # ---- 工作空间与主体 ----
    op.create_table(
        "workspace",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        _created_at(),
    )

    op.create_table(
        "agent",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text()),
        _created_at(),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("workspace_id", "name", name="uq_agent_workspace_id_name"),
    )
    op.create_index("ix_agent_workspace_id", "agent", ["workspace_id"])

    op.create_table(
        "end_user",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255)),
        _created_at(),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "workspace_id", "external_id", name="uq_end_user_workspace_id_external_id"
        ),
    )
    op.create_index("ix_end_user_workspace_id", "end_user", ["workspace_id"])

    op.create_table(
        "session",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column(
            "agent_id",
            UUID,
            sa.ForeignKey("agent.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "end_user_id",
            UUID,
            sa.ForeignKey("end_user.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("summary", sa.Text()),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _created_at(),
    )
    op.create_index("ix_session_workspace_id", "session", ["workspace_id"])

    # ---- 记忆事实源 ----
    op.create_table(
        "memory",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("scope_id", UUID),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("importance", sa.Float(), nullable=False, server_default=sa.text("0.5")),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("0.5")),
        sa.Column("stability", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "last_reinforced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("superseded_by", UUID, sa.ForeignKey("memory.id", ondelete="SET NULL")),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "source_type",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
        sa.Column("source_id", sa.String(length=64)),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("search_tsv", TSVECTOR),
        sa.Column("consolidation_run_id", UUID),
        _created_at(),
        _updated_at(),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "scope IN ('global', 'agent', 'user', 'session')",
            name="ck_memory_scope",
        ),
        sa.CheckConstraint(
            "type IN ('working', 'episodic', 'semantic', 'procedural', 'perceptual')",
            name="ck_memory_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived', 'superseded', 'deleted', 'purged')",
            name="ck_memory_status",
        ),
    )
    op.create_index("ix_memory_workspace_id", "memory", ["workspace_id"])
    op.create_index(
        "ix_memory_workspace_scope",
        "memory",
        ["workspace_id", "scope", "scope_id", "status", "type"],
    )
    op.create_index(
        "ix_memory_status_valid_to_last_reinforced",
        "memory",
        ["status", "valid_to", "last_reinforced_at"],
    )
    op.create_index(
        "ix_memory_search_tsv",
        "memory",
        ["search_tsv"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_memory_metadata",
        "memory",
        ["metadata"],
        postgresql_using="gin",
        postgresql_ops={"metadata": "jsonb_path_ops"},
    )

    op.create_table(
        "memory_embedding",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "memory_id", UUID, sa.ForeignKey("memory.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("vector", postgresql.ARRAY(sa.Float()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("memory_id", "model", "dim", name="uq_memory_embedding_memory_id_model_dim"),
    )

    op.create_table(
        "memory_link",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "from_id", UUID, sa.ForeignKey("memory.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "to_id", UUID, sa.ForeignKey("memory.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("relation", sa.String(length=64), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("evidence", JSONB),
        _created_at(),
    )
    op.create_index("ix_memory_link_from_id", "memory_link", ["from_id"])
    op.create_index("ix_memory_link_to_id", "memory_link", ["to_id"])

    op.create_table(
        "entity",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=64)),
        sa.Column("canonical_name", sa.String(length=255)),
        _created_at(),
        sa.UniqueConstraint("workspace_id", "name", name="uq_entity_workspace_id_name"),
    )
    op.create_index("ix_entity_workspace_id", "entity", ["workspace_id"])

    op.create_table(
        "memory_entity",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "memory_id", UUID, sa.ForeignKey("memory.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "entity_id", UUID, sa.ForeignKey("entity.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("role", sa.String(length=32)),
        sa.UniqueConstraint("memory_id", "entity_id", name="uq_memory_entity_memory_id_entity_id"),
    )

    op.create_table(
        "episode",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "session_id", UUID, sa.ForeignKey("session.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("title", sa.String(length=255)),
        sa.Column("summary", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(length=64)),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _created_at(),
    )
    op.create_index("ix_episode_session_id", "episode", ["session_id"])

    # ---- 运营与一致性 ----
    op.create_table(
        "review_item",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("candidate", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("evidence", JSONB),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("0.5")),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")
        ),
        _created_at(),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_review_item_workspace_status", "review_item", ["workspace_id", "status"]
    )

    op.create_table(
        "job_run",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=255)),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("params", JSONB),
        sa.Column("result", JSONB),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        _created_at(),
    )
    op.create_index("ix_job_run_type_status", "job_run", ["type", "status"])

    op.create_table(
        "audit_log",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("workspace_id", UUID),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", UUID),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("before", JSONB),
        sa.Column("after", JSONB),
        _created_at(),
    )
    op.execute(
        "CREATE INDEX ix_audit_log_target ON audit_log "
        "(target_type, target_id, created_at DESC)"
    )

    op.create_table(
        "outbox_event",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "aggregate_type",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'memory'"),
        ),
        sa.Column("aggregate_id", UUID, nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        _created_at(),
    )
    op.create_index(
        "ix_outbox_event_status_next_attempt_at",
        "outbox_event",
        ["status", "next_attempt_at"],
    )

    op.create_table(
        "projection_state",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "memory_id", UUID, sa.ForeignKey("memory.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("target", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "projected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("error", sa.Text()),
        sa.UniqueConstraint("memory_id", "target", name="uq_projection_state_memory_id_target"),
    )

    # ---- 认证与 Provider ----
    op.create_table(
        "api_key",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=255), nullable=False),
        sa.Column("scopes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("rate_limit", sa.Integer()),
        _created_at(),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("prefix", name="uq_api_key_prefix"),
    )
    op.create_index("ix_api_key_workspace_id", "api_key", ["workspace_id"])

    op.create_table(
        "provider_config",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("base_url", sa.String(length=1024)),
        sa.Column("model", sa.String(length=255)),
        sa.Column("dim", sa.Integer()),
        sa.Column("extra", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("encrypted_secret", sa.LargeBinary()),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.UniqueConstraint("kind", "name", name="uq_provider_config_kind_name"),
    )

    # ---- 触发器：全文检索 + 版本自增 ----
    op.execute(
        """
        CREATE OR REPLACE FUNCTION memory_refresh_search_tsv() RETURNS trigger AS $$
        DECLARE
            cfg regconfig;
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'zh') THEN
                cfg := 'zh'::regconfig;
            ELSE
                cfg := 'simple'::regconfig;
            END IF;
            NEW.search_tsv := to_tsvector(
                cfg,
                coalesce(NEW.content, '') || ' ' || coalesce(NEW.summary, '')
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER memory_search_tsv_trigger
        BEFORE INSERT OR UPDATE OF content, summary ON memory
        FOR EACH ROW EXECUTE FUNCTION memory_refresh_search_tsv();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION memory_bump_version() RETURNS trigger AS $$
        BEGIN
            -- ORM 已显式 +1 时保留；任何绕过 ORM 的更新也至少自增一次。
            IF NEW.version IS NULL OR NEW.version <= OLD.version THEN
                NEW.version := OLD.version + 1;
            END IF;
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER memory_bump_version_trigger
        BEFORE UPDATE ON memory
        FOR EACH ROW EXECUTE FUNCTION memory_bump_version();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS memory_bump_version_trigger ON memory")
    op.execute("DROP FUNCTION IF EXISTS memory_bump_version()")
    op.execute("DROP TRIGGER IF EXISTS memory_search_tsv_trigger ON memory")
    op.execute("DROP FUNCTION IF EXISTS memory_refresh_search_tsv()")

    op.drop_table("provider_config")
    op.drop_index("ix_api_key_workspace_id", table_name="api_key")
    op.drop_table("api_key")
    op.drop_table("projection_state")
    op.drop_index("ix_outbox_event_status_next_attempt_at", table_name="outbox_event")
    op.drop_table("outbox_event")
    op.execute("DROP INDEX IF EXISTS ix_audit_log_target")
    op.drop_table("audit_log")
    op.drop_index("ix_job_run_type_status", table_name="job_run")
    op.drop_table("job_run")
    op.drop_index("ix_review_item_workspace_status", table_name="review_item")
    op.drop_table("review_item")
    op.drop_index("ix_episode_session_id", table_name="episode")
    op.drop_table("episode")
    op.drop_table("memory_entity")
    op.drop_index("ix_entity_workspace_id", table_name="entity")
    op.drop_table("entity")
    op.drop_index("ix_memory_link_to_id", table_name="memory_link")
    op.drop_index("ix_memory_link_from_id", table_name="memory_link")
    op.drop_table("memory_link")
    op.drop_table("memory_embedding")
    op.drop_index("ix_memory_metadata", table_name="memory")
    op.drop_index("ix_memory_search_tsv", table_name="memory")
    op.drop_index("ix_memory_status_valid_to_last_reinforced", table_name="memory")
    op.drop_index("ix_memory_workspace_scope", table_name="memory")
    op.drop_index("ix_memory_workspace_id", table_name="memory")
    op.drop_table("memory")
    op.drop_index("ix_session_workspace_id", table_name="session")
    op.drop_table("session")
    op.drop_index("ix_end_user_workspace_id", table_name="end_user")
    op.drop_table("end_user")
    op.drop_index("ix_agent_workspace_id", table_name="agent")
    op.drop_table("agent")
    op.drop_table("workspace")

    # 仅回收本迁移创建的扩展；zhparser 可能被共享，故保留。
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
