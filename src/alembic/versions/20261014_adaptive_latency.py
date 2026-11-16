"""Store provider-attempt latency observations and model state.

Revision ID: 20261014_adaptive_latency
Revises: 20261012_application_name_unique
"""

import sqlalchemy as sa

from alembic import op

revision = "20261014_adaptive_latency"
down_revision = "20261012_application_name_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "latency_observations",
        sa.Column("attempt_id", sa.String(length=36), primary_key=True),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=True),
        sa.Column("application_id", sa.String(length=36), nullable=True),
        sa.Column("model_key_hash", sa.String(length=64), nullable=False),
        sa.Column("catalog_model_id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.String(length=36), nullable=False),
        sa.Column("provider_kind", sa.String(length=50), nullable=False),
        sa.Column("upstream_model_name", sa.String(length=300), nullable=False),
        sa.Column("connection_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("profile_key", sa.String(length=64), nullable=False),
        sa.Column("feature_schema", sa.String(length=30), nullable=False),
        sa.Column("features_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("execution_mode", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_total_ms", sa.Float(), nullable=True),
        sa.Column("ttft_ms", sa.Float(), nullable=True),
        sa.Column("consumer_pause_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("catalog_ms", sa.Float(), nullable=True),
        sa.Column("predicted_ms", sa.Float(), nullable=True),
        sa.Column("prediction_version", sa.String(length=80), nullable=True),
        sa.Column("error_category", sa.String(length=80), nullable=True),
        sa.Column("training_eligible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("actual_payload_chars", sa.Integer(), nullable=True),
        sa.Column("resolution_source", sa.String(length=20), nullable=True),
        sa.Column("resolution_reason", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('success', 'error', 'timeout', 'cancelled', 'incomplete')",
            name="ck_latency_observations_status",
        ),
        sa.CheckConstraint("provider_total_ms IS NULL OR provider_total_ms > 0", name="ck_latency_observations_duration"),
    )
    op.create_index(
        "ix_latency_observations_group_finished", "latency_observations", ["model_key_hash", "finished_at"]
    )
    op.create_index("ix_latency_observations_finished", "latency_observations", ["finished_at"])
    op.create_index("ix_latency_observations_request_id", "latency_observations", ["request_id"])

    op.create_table(
        "latency_training_runs",
        sa.Column("run_id", sa.String(length=36), primary_key=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("owner", sa.String(length=100), nullable=False),
        sa.Column("lease_token", sa.String(length=36), nullable=False),
        sa.Column("fencing_generation", sa.Integer(), nullable=False),
        sa.Column("expected_active_generation", sa.Integer(), nullable=False),
        sa.Column("dataset_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dataset_digest", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("feature_schema", sa.String(length=30), nullable=False, server_default="latency-v1"),
        sa.Column("candidate_version", sa.String(length=80), nullable=True),
        sa.Column("artifact_json", sa.JSON(), nullable=True),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'published', 'rejected', 'failed', 'abandoned')",
            name="ck_latency_training_runs_status",
        ),
    )
    op.create_index("ix_latency_training_runs_created", "latency_training_runs", ["created_at"])

    op.create_table(
        "latency_model_state",
        sa.Column("scope", sa.String(length=30), primary_key=True),
        sa.Column("active_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_version", sa.String(length=80), nullable=True),
        sa.Column("active_artifact_json", sa.JSON(), nullable=True),
        sa.Column("active_run_id", sa.String(length=36), nullable=True),
        sa.Column("previous_versions_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("training_watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("scope = 'generation-v1'", name="ck_latency_model_state_scope"),
    )
    op.create_index("ix_latency_model_state_lease_expiry", "latency_model_state", ["lease_expires_at"])
    op.bulk_insert(
        sa.table("latency_model_state", sa.column("scope", sa.String())),
        [{"scope": "generation-v1"}],
    )

    if op.get_bind().dialect.name == "postgresql":
        for table in ("latency_observations", "latency_training_runs", "latency_model_state"):
            op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(
                "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN "
                f'REVOKE ALL PRIVILEGES ON TABLE public."{table}" FROM anon; END IF; END $$;'
            )
            op.execute(
                "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN "
                f'REVOKE ALL PRIVILEGES ON TABLE public."{table}" FROM authenticated; END IF; END $$;'
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in ("latency_model_state", "latency_training_runs", "latency_observations"):
            op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
    op.drop_index("ix_latency_model_state_lease_expiry", table_name="latency_model_state")
    op.drop_table("latency_model_state")
    op.drop_index("ix_latency_training_runs_created", table_name="latency_training_runs")
    op.drop_table("latency_training_runs")
    op.drop_index("ix_latency_observations_request_id", table_name="latency_observations")
    op.drop_index("ix_latency_observations_finished", table_name="latency_observations")
    op.drop_index("ix_latency_observations_group_finished", table_name="latency_observations")
    op.drop_table("latency_observations")
