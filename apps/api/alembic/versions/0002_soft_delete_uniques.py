"""软删除与自然键唯一性：改为 partial unique index。

``agent(workspace_id, name)`` 与 ``end_user(workspace_id, external_id)`` 在
0001 中是全量唯一约束，但删除是软删除（``deleted_at`` 非空），导致同名
Agent / 同 external_id 的终端用户无法重建。这里把唯一性收窄到未删除行。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_soft_delete_uniques"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_agent_workspace_id_name", "agent", type_="unique")
    op.create_index(
        "uq_agent_workspace_id_name_active",
        "agent",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.drop_constraint(
        "uq_end_user_workspace_id_external_id", "end_user", type_="unique"
    )
    op.create_index(
        "uq_end_user_workspace_id_external_id_active",
        "end_user",
        ["workspace_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_end_user_workspace_id_external_id_active", table_name="end_user")
    op.create_unique_constraint(
        "uq_end_user_workspace_id_external_id", "end_user", ["workspace_id", "external_id"]
    )

    op.drop_index("uq_agent_workspace_id_name_active", table_name="agent")
    op.create_unique_constraint(
        "uq_agent_workspace_id_name", "agent", ["workspace_id", "name"]
    )
