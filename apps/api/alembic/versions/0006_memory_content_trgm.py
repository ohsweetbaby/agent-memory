"""为中文关键词/相似度检索补 trigram GIN 索引。

M1 的关键词通道在无 zhparser 时走 token 级 ILIKE + pg_trgm；没有索引时
1 万条数据会退化为全表扫描，无法满足 p95 < 300ms 的验收线。
"""

from __future__ import annotations

from alembic import op

revision = "0006_memory_content_trgm"
down_revision = "0005_projection_target_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_content_trgm "
        "ON memory USING gin (content gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memory_content_trgm")
