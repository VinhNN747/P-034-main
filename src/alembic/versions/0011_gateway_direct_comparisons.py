"""Persist Gateway versus direct-model comparison runs.

Revision ID: 0011_gateway_direct_comparisons
Revises: 0010_merge_gateway_heads
"""

import sqlalchemy as sa

from alembic import op

revision = "0011_gateway_direct_comparisons"
down_revision = "0010_merge_gateway_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comparison_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("owner_user_id", sa.String(100), nullable=False),
        sa.Column("gateway_request_id", sa.String(128), sa.ForeignKey("gateway_requests.request_id"), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="playground"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("execution_mode", sa.String(20), nullable=False, server_default="offline"),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("constraints_json", sa.JSON(), nullable=False),
        sa.Column("strategies_json", sa.JSON(), nullable=False),
        sa.Column("reference_model_ids_json", sa.JSON(), nullable=False),
        sa.Column("model_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("rubric_json", sa.JSON(), nullable=False),
        sa.Column("quality_evaluation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("evaluator_model_id", sa.String(36), nullable=True),
        sa.Column("max_comparison_cost_usd", sa.Float(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("application_id", "idempotency_key", name="uq_comparison_runs_app_idempotency"),
    )
    op.create_index("ix_comparison_runs_application_created", "comparison_runs", ["application_id", "created_at"])
    op.create_index("ix_comparison_runs_status_lease", "comparison_runs", ["status", "lease_expires_at"])
    op.create_table(
        "comparison_branches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("comparison_id", sa.String(36), sa.ForeignKey("comparison_runs.id"), nullable=False),
        sa.Column("branch_key", sa.String(100), nullable=False),
        sa.Column("branch_type", sa.String(20), nullable=False),
        sa.Column("model_id", sa.String(36), nullable=True),
        sa.Column("model_name", sa.String(150), nullable=True),
        sa.Column("provider_name", sa.String(100), nullable=True),
        sa.Column("provider_kind", sa.String(50), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("usage_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pricing_estimated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("input_price_per_million", sa.Float(), nullable=True),
        sa.Column("output_price_per_million", sa.Float(), nullable=True),
        sa.Column("pricing_version", sa.String(100), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("queue_delay_ms", sa.Float(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("comparison_id", "branch_key", name="uq_comparison_branches_key"),
    )
    op.create_index("ix_comparison_branches_comparison", "comparison_branches", ["comparison_id"])
    op.create_table(
        "comparison_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("branch_id", sa.String(36), sa.ForeignKey("comparison_branches.id"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.String(150), nullable=True),
        sa.Column("provider_name", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("usage_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("latency_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_comparison_attempts_branch", "comparison_attempts", ["branch_id", "attempt_number"])
    op.create_table(
        "comparison_evaluations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("comparison_id", sa.String(36), sa.ForeignKey("comparison_runs.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("evaluator_model_id", sa.String(36), nullable=True),
        sa.Column("evaluator_model_name", sa.String(150), nullable=True),
        sa.Column("rubric_version", sa.String(50), nullable=False, server_default="default-v1"),
        sa.Column("scores_json", sa.JSON(), nullable=False),
        sa.Column("rationale_json", sa.JSON(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("usage_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("comparison_id", name="uq_comparison_evaluations_run"),
    )


def downgrade() -> None:
    op.drop_table("comparison_evaluations")
    op.drop_index("ix_comparison_attempts_branch", table_name="comparison_attempts")
    op.drop_table("comparison_attempts")
    op.drop_index("ix_comparison_branches_comparison", table_name="comparison_branches")
    op.drop_table("comparison_branches")
    op.drop_index("ix_comparison_runs_status_lease", table_name="comparison_runs")
    op.drop_index("ix_comparison_runs_application_created", table_name="comparison_runs")
    op.drop_table("comparison_runs")
