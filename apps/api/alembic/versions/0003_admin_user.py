"""管理员账号表。

规格 §9.3 只要求“首次启动创建管理员”，未给出表结构；这里补充
``admin_user``（UUIDv7 主键、Argon2id 密码哈希）。M1 只支持单管理员，
但保留 username 唯一约束以便后续扩展。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_admin_user"
down_revision: str | None = "0002_soft_delete_uniques"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = sa.String(length=36)


def upgrade() -> None:
    op.create_table(
        "admin_user",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("username", name="uq_admin_user_username"),
    )


def downgrade() -> None:
    op.drop_table("admin_user")
