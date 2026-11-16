"""Persist Gateway requests, execution and observability data.

Revision ID: 0004_gateway_runtime
Revises: 0003_gateway_catalog
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_gateway_runtime"
down_revision = "0003_gateway_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gateway_requests",
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("messages_json", sa.JSON(), nullable=False),
        sa.Column("constraints_json", sa.JSON(), nullable=False),
        sa.Column("strategies_json", sa.JSON(), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("aggregation_status", sa.String(length=20), nullable=True),
        sa.Column("total_cost", sa.Float(), nullable=False),
        sa.Column("total_latency_ms", sa.Float(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index(
        "ix_gateway_requests_application_started",
        "gateway_requests",
        ["application_id", "started_at"],
    )
    op.create_table(
        "gateway_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("task_type", sa.String(length=50), nullable=False),
        sa.Column("difficulty", sa.String(length=20), nullable=False),
        sa.Column("dependencies_json", sa.JSON(), nullable=False),
        sa.Column("input_from_json", sa.JSON(), nullable=False),
        sa.Column("required_capabilities_json", sa.JSON(), nullable=False),
        sa.Column("constraints_json", sa.JSON(), nullable=False),
        sa.Column("expected_output", sa.Text(), nullable=False),
        sa.Column("goal_ids_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["gateway_requests.request_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", "task_id", name="uq_gateway_tasks_request_task"),
    )
    op.create_index("ix_gateway_tasks_request", "gateway_tasks", ["request_id"])
    op.create_table(
        "route_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("strategy", sa.String(length=80), nullable=False),
        sa.Column("selected_tier", sa.String(length=150), nullable=True),
        sa.Column("selected_model", sa.String(length=150), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("fallback_models_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["request_id"], ["gateway_requests.request_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", "task_id", name="uq_route_decisions_request_task"),
    )
    op.create_index("ix_route_decisions_model", "route_decisions", ["selected_model"])
    op.create_table(
        "model_executions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=150), nullable=True),
        sa.Column("selected_model", sa.String(length=150), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("fallback", sa.Boolean(), nullable=False),
        sa.Column("failure_type", sa.String(length=100), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("result_payload", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["request_id"], ["gateway_requests.request_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_executions_request_task", "model_executions", ["request_id", "task_id"])
    op.create_index("ix_model_executions_provider_model", "model_executions", ["provider", "model"])
    op.create_table(
        "aggregation_results",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("strategy", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("final_answer", sa.Text(), nullable=True),
        sa.Column("contributions_json", sa.JSON(), nullable=False),
        sa.Column("missing_goal_ids_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["gateway_requests.request_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_aggregation_results_request"),
    )
    op.create_table(
        "stage_traces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("stage", sa.String(length=100), nullable=False),
        sa.Column("strategy", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=150), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["gateway_requests.request_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stage_traces_request_started", "stage_traces", ["request_id", "started_at"])
    op.create_index("ix_stage_traces_stage_status", "stage_traces", ["stage", "status"])
    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=150), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("estimated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.ForeignKeyConstraint(["request_id"], ["gateway_requests.request_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_usage_events_request_created", "usage_events", ["request_id", "created_at"])
    op.create_table(
        "incidents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=150), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_incidents_status_created", "incidents", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_incidents_status_created", table_name="incidents")
    op.drop_table("incidents")
    op.drop_index("ix_usage_events_request_created", table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_index("ix_stage_traces_stage_status", table_name="stage_traces")
    op.drop_index("ix_stage_traces_request_started", table_name="stage_traces")
    op.drop_table("stage_traces")
    op.drop_table("aggregation_results")
    op.drop_index("ix_model_executions_provider_model", table_name="model_executions")
    op.drop_index("ix_model_executions_request_task", table_name="model_executions")
    op.drop_table("model_executions")
    op.drop_index("ix_route_decisions_model", table_name="route_decisions")
    op.drop_table("route_decisions")
    op.drop_index("ix_gateway_tasks_request", table_name="gateway_tasks")
    op.drop_table("gateway_tasks")
    op.drop_index("ix_gateway_requests_application_started", table_name="gateway_requests")
    op.drop_table("gateway_requests")
