"""Persist server-owned support conversation slots.

Revision ID: 0012_support_chat_context
Revises: 0011_gateway_direct_comparisons
"""

import sqlalchemy as sa

from alembic import op

revision = "0012_support_chat_context"
down_revision = "0011_gateway_direct_comparisons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("support_chat_sessions", sa.Column("state_json", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("support_chat_messages", sa.Column("application_id", sa.String(100), nullable=True))
    op.create_table(
        "support_chat_rate_limits",
        sa.Column("user_id", sa.String(100), primary_key=True),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE public.support_chat_rate_limits ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL PRIVILEGES ON TABLE public.support_chat_rate_limits FROM anon, authenticated")


def downgrade() -> None:
    op.drop_table("support_chat_rate_limits")
    op.drop_column("support_chat_messages", "application_id")
    op.drop_column("support_chat_sessions", "state_json")
