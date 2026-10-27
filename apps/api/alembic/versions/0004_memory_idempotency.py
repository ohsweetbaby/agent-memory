"""记忆写入幂等键。

任务 7 的 ``memory`` 表没有幂等键；任务 9 需要保证并发提交同一个
``idempotency_key`` 时只落一条记忆。这里新增 nullable 列，并把唯一性收窄到
非空 key，未使用幂等的写入保持原行为。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_memory_idempotency"
down_revision: str | None = "0003_admin_user"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory", sa.Column("idempotency_key", sa.String(length=255), nullable=True)
    )
    op.create_index(
        "uq_memory_workspace_idempotency_key",
        "memory",
        ["workspace_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_memory_workspace_idempotency_key", table_name="memory")
    op.drop_column("memory", "idempotency_key")
