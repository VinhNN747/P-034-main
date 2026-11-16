"""Create Gateway catalog and application binding tables.

Revision ID: 0003_gateway_catalog
Revises: 0002_applications
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_gateway_catalog"
down_revision = "0002_applications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "providers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("secret_env_var", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "models",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("target_latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_price_per_million", sa.Float(), nullable=False),
        sa.Column("output_price_per_million", sa.Float(), nullable=False),
        sa.Column("pricing_version", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_id", "name", name="uq_models_provider_name"),
    )
    op.create_table(
        "model_pools",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("purpose", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("routing_strategy", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "model_pool_members",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("pool_id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"]),
        sa.ForeignKeyConstraint(["pool_id"], ["model_pools.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pool_id", "model_id", name="uq_pool_model"),
        sa.UniqueConstraint("pool_id", "priority", name="uq_pool_priority"),
    )
    op.create_table(
        "routing_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("match_conditions_json", sa.JSON(), nullable=False),
        sa.Column("pool_id", sa.String(length=36), nullable=False),
        sa.Column("guardrails_json", sa.JSON(), nullable=False),
        sa.Column("fallback_chain_json", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["pool_id"], ["model_pools.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "application_gateway_configs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("model_pool_id", sa.String(length=36), nullable=False),
        sa.Column("routing_policy_id", sa.String(length=36), nullable=False),
        sa.Column("budget_ceiling", sa.Float(), nullable=True),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("fallback_model_id", sa.String(length=36), nullable=True),
        sa.Column("default_understander", sa.String(length=50), nullable=False),
        sa.Column("default_decomposer", sa.String(length=50), nullable=False),
        sa.Column("default_router", sa.String(length=50), nullable=False),
        sa.Column("default_aggregator", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.ForeignKeyConstraint(["fallback_model_id"], ["models.id"]),
        sa.ForeignKeyConstraint(["model_pool_id"], ["model_pools.id"]),
        sa.ForeignKeyConstraint(["routing_policy_id"], ["routing_policies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id"),
    )


def downgrade() -> None:
    op.drop_table("application_gateway_configs")
    op.drop_table("routing_policies")
    op.drop_table("model_pool_members")
    op.drop_table("model_pools")
    op.drop_table("models")
    op.drop_table("providers")
