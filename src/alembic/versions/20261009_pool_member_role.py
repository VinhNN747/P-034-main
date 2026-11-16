"""Pool members get a role: one primary model per tier plus optional fallbacks.

Existing rows become "primary", so current pools route exactly as before.

Revision ID: 20261009_pool_member_role
Revises: 20261008_support_analytics
"""

import sqlalchemy as sa

from alembic import op

revision = "20261009_pool_member_role"
down_revision = "20261008_support_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_pool_members",
        sa.Column("role", sa.String(length=20), nullable=False, server_default="primary"),
    )


def downgrade() -> None:
    op.drop_column("model_pool_members", "role")
