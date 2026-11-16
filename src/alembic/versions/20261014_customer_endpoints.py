"""Customer-owned provider connections (bring your own OpenAI-compatible endpoint).

Revision ID: 20261014_customer_endpoints
Revises: 20261014_budget_alerts
"""

import sqlalchemy as sa

from alembic import op

revision = "20261014_customer_endpoints"
down_revision = "20261014_budget_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL keeps every existing connection a system connection: no data change.
    op.add_column("providers", sa.Column("owner_user_id", sa.String(length=100), nullable=True))
    op.create_index("ix_providers_owner_user_id", "providers", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_providers_owner_user_id", table_name="providers")
    with op.batch_alter_table("providers") as batch_op:
        batch_op.drop_column("owner_user_id")
