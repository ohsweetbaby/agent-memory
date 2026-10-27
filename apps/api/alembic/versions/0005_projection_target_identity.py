"""投影幂等键包含索引身份。

``projection_state.target`` 从 32 扩到 255，用于容纳
``qdrant:mem_{provider}_{model}_{dim}`` 这类带 collection 身份的目标名。
切换 provider/model/dim 后会使用新的 target，旧状态不会阻止新 collection
回填。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_projection_target_identity"
down_revision: str | None = "0004_memory_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "projection_state",
        "target",
        existing_type=sa.String(length=32),
        type_=sa.String(length=255),
        existing_nullable=False,
    )


def downgrade() -> None:
    # 降级前先截断超长 target；M1 回滚场景只用于空表/短值。
    op.execute(
        "UPDATE projection_state SET target = left(target, 32) "
        "WHERE length(target) > 32"
    )
    op.alter_column(
        "projection_state",
        "target",
        existing_type=sa.String(length=255),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
