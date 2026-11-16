"""Add FE integration domain, billing and audit persistence.

Revision ID: 0005_fe_integration_domain
Revises: 0004_gateway_runtime
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_fe_integration_domain"
down_revision = "0004_gateway_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_gateway_configs",
        sa.Column(
            "routing_objective",
            sa.String(length=50),
            nullable=False,
            server_default="balanced",
        ),
    )
    op.add_column(
        "application_gateway_configs",
        sa.Column(
            "fallback_model_ids_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "application_gateway_configs",
        sa.Column(
            "warning_threshold",
            sa.Float(),
            nullable=False,
            server_default="0.8",
        ),
    )
    op.add_column(
        "application_gateway_configs",
        sa.Column(
            "budget_action",
            sa.String(length=20),
            nullable=False,
            server_default="block",
        ),
    )
    op.add_column(
        "application_gateway_configs",
        sa.Column(
            "max_output_tokens",
            sa.Integer(),
            nullable=False,
            server_default="800",
        ),
    )
    op.add_column(
        "application_gateway_configs",
        sa.Column(
            "timeout_seconds",
            sa.Float(),
            nullable=False,
            server_default="30",
        ),
    )
    op.add_column(
        "application_gateway_configs",
        sa.Column(
            "retry_limit",
            sa.Integer(),
            nullable=False,
            server_default="2",
        ),
    )
    op.add_column(
        "application_gateway_configs",
        sa.Column(
            "fallback_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "application_gateway_configs",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    op.add_column(
        "models",
        sa.Column(
            "quality_tier",
            sa.String(length=30),
            nullable=False,
            server_default="standard",
        ),
    )
    op.add_column(
        "models",
        sa.Column(
            "health_status",
            sa.String(length=30),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column("models", sa.Column("last_probe_at", sa.DateTime(timezone=True)))
    op.add_column("models", sa.Column("last_probe_latency_ms", sa.Float()))
    op.add_column("models", sa.Column("last_probe_error", sa.Text()))

    op.add_column(
        "gateway_requests",
        sa.Column(
            "source",
            sa.String(length=20),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "gateway_requests",
        sa.Column(
            "fallback_used",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column("gateway_requests", sa.Column("fallback_reason", sa.Text()))
    op.add_column(
        "gateway_requests",
        sa.Column("billing_transaction_id", sa.String(length=36)),
    )
    op.create_index(
        "ix_gateway_requests_status_source",
        "gateway_requests",
        ["status", "source"],
    )

    op.add_column(
        "usage_events",
        sa.Column(
            "source",
            sa.String(length=20),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "usage_events",
        sa.Column("billing_transaction_id", sa.String(length=36)),
    )
    op.create_index(
        "ix_usage_events_application_source_created",
        "usage_events",
        ["application_id", "source", "created_at"],
    )

    op.create_table(
        "application_api_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_prefix", name="uq_application_api_keys_prefix"),
    )
    op.create_index(
        "ix_application_api_keys_application",
        "application_api_keys",
        ["application_id"],
    )

    op.create_table(
        "policy_reviews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36)),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("scope", sa.String(length=50), nullable=False),
        sa.Column("review_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("submitted_by", sa.String(length=100), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("affected_models_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_policy_reviews_status_submitted",
        "policy_reviews",
        ["status", "submitted_at"],
    )
    op.create_index(
        "ix_policy_reviews_application",
        "policy_reviews",
        ["application_id"],
    )

    op.create_table(
        "policy_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=100), nullable=False),
        sa.Column("decision", sa.String(length=30), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["review_id"], ["policy_reviews.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_policy_decisions_review_created",
        "policy_decisions",
        ["review_id", "created_at"],
    )

    op.create_table(
        "billing_accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=100), nullable=False),
        sa.Column("balance", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("lifetime_topup", sa.Numeric(18, 6), nullable=False),
        sa.Column("lifetime_usage", sa.Numeric(18, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_billing_accounts_user"),
    )

    op.create_table(
        "billing_settings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("low_balance_threshold", sa.Numeric(18, 6), nullable=False),
        sa.Column("notify_low_balance", sa.Boolean(), nullable=False),
        sa.Column("auto_topup_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["billing_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", name="uq_billing_settings_account"),
    )

    op.create_table(
        "billing_transactions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("transaction_type", sa.String(length=30), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128)),
        sa.Column("source_request_id", sa.String(length=128)),
        sa.Column("reference_type", sa.String(length=50)),
        sa.Column("reference_id", sa.String(length=128)),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["billing_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_billing_transactions_idempotency"
        ),
        sa.UniqueConstraint(
            "source_request_id", name="uq_billing_transactions_request"
        ),
    )
    op.create_index(
        "ix_billing_transactions_account_created",
        "billing_transactions",
        ["account_id", "created_at"],
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=100)),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.String(length=128)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_resource_created",
        "audit_events",
        ["resource_type", "resource_id", "created_at"],
    )
    op.create_index(
        "ix_audit_events_actor_created",
        "audit_events",
        ["actor_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_actor_created", table_name="audit_events")
    op.drop_index("ix_audit_events_resource_created", table_name="audit_events")
    op.drop_table("audit_events")

    op.drop_index(
        "ix_billing_transactions_account_created",
        table_name="billing_transactions",
    )
    op.drop_table("billing_transactions")
    op.drop_table("billing_settings")
    op.drop_table("billing_accounts")

    op.drop_index(
        "ix_policy_decisions_review_created",
        table_name="policy_decisions",
    )
    op.drop_table("policy_decisions")
    op.drop_index("ix_policy_reviews_application", table_name="policy_reviews")
    op.drop_index(
        "ix_policy_reviews_status_submitted",
        table_name="policy_reviews",
    )
    op.drop_table("policy_reviews")

    op.drop_index(
        "ix_application_api_keys_application",
        table_name="application_api_keys",
    )
    op.drop_table("application_api_keys")

    op.drop_index(
        "ix_usage_events_application_source_created",
        table_name="usage_events",
    )
    op.drop_column("usage_events", "billing_transaction_id")
    op.drop_column("usage_events", "source")

    op.drop_index(
        "ix_gateway_requests_status_source",
        table_name="gateway_requests",
    )
    op.drop_column("gateway_requests", "billing_transaction_id")
    op.drop_column("gateway_requests", "fallback_reason")
    op.drop_column("gateway_requests", "fallback_used")
    op.drop_column("gateway_requests", "source")

    op.drop_column("models", "last_probe_error")
    op.drop_column("models", "last_probe_latency_ms")
    op.drop_column("models", "last_probe_at")
    op.drop_column("models", "health_status")
    op.drop_column("models", "quality_tier")

    op.drop_column("application_gateway_configs", "version")
    op.drop_column("application_gateway_configs", "fallback_enabled")
    op.drop_column("application_gateway_configs", "retry_limit")
    op.drop_column("application_gateway_configs", "timeout_seconds")
    op.drop_column("application_gateway_configs", "max_output_tokens")
    op.drop_column("application_gateway_configs", "budget_action")
    op.drop_column("application_gateway_configs", "warning_threshold")
    op.drop_column("application_gateway_configs", "fallback_model_ids_json")
    op.drop_column("application_gateway_configs", "routing_objective")
