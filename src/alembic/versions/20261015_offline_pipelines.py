"""Offline pipelines: the pipeline is picked per application (request), not per process.

Revision ID: 20261015_offline_pipelines
Revises: 20261014_customer_endpoints

Run after docs/migration-customer-endpoints.md: the chain must be joined to 20261014_budget_alerts first.
"""

import sqlalchemy as sa

from alembic import op

revision = "20261015_offline_pipelines"
down_revision = "20261014_customer_endpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every existing application keeps the online pipeline: no data change.
    op.add_column(
        "applications",
        sa.Column("pipeline", sa.String(length=20), nullable=False, server_default="online"),
    )
    op.add_column("application_gateway_configs", sa.Column("classifier_mid_difficulty", sa.Float(), nullable=True))
    op.add_column("application_gateway_configs", sa.Column("classifier_large_difficulty", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("application_gateway_configs") as batch_op:
        batch_op.drop_column("classifier_large_difficulty")
        batch_op.drop_column("classifier_mid_difficulty")
    with op.batch_alter_table("applications") as batch_op:
        batch_op.drop_column("pipeline")
