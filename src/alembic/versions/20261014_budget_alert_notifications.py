"""Add budget alert delivery idempotency."""

import sqlalchemy as sa

from alembic import op

revision = "20261014_budget_alerts"
down_revision = "20261014_adaptive_latency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budget_alert_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("alert_level", sa.String(length=20), nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provider_message_id", sa.String(length=200), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "application_id",
            "period_start",
            "alert_level",
            "recipient",
            name="uq_budget_alert_delivery_key",
        ),
    )
    op.create_index(
        "ix_budget_alert_deliveries_application_period",
        "budget_alert_deliveries",
        ["application_id", "period_start"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_budget_alert_deliveries_application_period",
        table_name="budget_alert_deliveries",
    )
    op.drop_table("budget_alert_deliveries")
