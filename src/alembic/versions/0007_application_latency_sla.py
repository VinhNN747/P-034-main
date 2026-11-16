"""Add application-level model latency SLA.

Revision ID: 0007_application_latency_sla
Revises: 0006_secure_domain_tables
"""

import sqlalchemy as sa

from alembic import op

revision = "0007_application_latency_sla"
down_revision = "0006_secure_domain_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_gateway_configs",
        sa.Column("max_latency_ms", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("application_gateway_configs", "max_latency_ms")
