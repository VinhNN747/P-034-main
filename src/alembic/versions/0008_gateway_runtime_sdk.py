"""Add application-key runtime idempotency receipts.

Revision ID: 0008_gateway_runtime_sdk
Revises: 0007_application_latency_sla
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_gateway_runtime_sdk"
down_revision = "0007_application_latency_sla"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_operations",
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("application_api_key_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("normalization_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("execution_mode", sa.String(length=20), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("config_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("public_result_json", sa.JSON(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("http_body_json", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.ForeignKeyConstraint(["application_api_key_id"], ["application_api_keys.id"]),
        sa.PrimaryKeyConstraint("request_id"),
        sa.UniqueConstraint("application_id", "idempotency_key_hash", name="uq_runtime_operations_app_key"),
    )
    op.create_index(
        "ix_runtime_operations_application_started", "runtime_operations", ["application_id", "started_at"]
    )
    op.create_index(
        "ix_runtime_operations_application_status_expiry",
        "runtime_operations",
        ["application_id", "status", "expires_at"],
    )

    with op.batch_alter_table("gateway_requests") as batch_op:
        batch_op.add_column(sa.Column("application_api_key_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("config_version", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_gateway_requests_application_api_key_id",
            "application_api_keys",
            ["application_api_key_id"],
            ["id"],
        )
        batch_op.create_index("ix_gateway_requests_api_key_started", ["application_api_key_id", "started_at"])

    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE public."runtime_operations" ENABLE ROW LEVEL SECURITY')
        op.execute('REVOKE ALL PRIVILEGES ON TABLE public."runtime_operations" FROM anon, authenticated')


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute('GRANT ALL PRIVILEGES ON TABLE public."runtime_operations" TO anon, authenticated')
        op.execute('ALTER TABLE public."runtime_operations" DISABLE ROW LEVEL SECURITY')

    with op.batch_alter_table("gateway_requests") as batch_op:
        batch_op.drop_index("ix_gateway_requests_api_key_started")
        batch_op.drop_constraint("fk_gateway_requests_application_api_key_id", type_="foreignkey")
        batch_op.drop_column("config_version")
        batch_op.drop_column("application_api_key_id")

    op.drop_index("ix_runtime_operations_application_status_expiry", table_name="runtime_operations")
    op.drop_index("ix_runtime_operations_application_started", table_name="runtime_operations")
    op.drop_table("runtime_operations")
