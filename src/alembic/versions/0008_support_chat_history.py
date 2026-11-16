"""Store per-user support chat sessions and messages.

Revision ID: 0008_support_chat_history
Revises: 0007_application_latency_sla
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_support_chat_history"
down_revision = "0007_application_latency_sla"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_chat_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(100), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", "user_id", name="uq_support_chat_session_owner"),
    )
    op.create_index(
        "ix_support_chat_sessions_user_updated",
        "support_chat_sessions",
        ["user_id", "updated_at"],
    )
    op.create_table(
        "support_chat_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(100), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=True),
        sa.Column("required_permission", sa.String(100), nullable=True),
        sa.Column("sources_json", sa.JSON(), nullable=False),
        sa.Column("actions_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id", "user_id"],
            ["support_chat_sessions.id", "support_chat_sessions.user_id"],
            name="fk_support_chat_message_owned_session",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_support_chat_messages_session_created",
        "support_chat_messages",
        ["user_id", "session_id", "created_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        for table_name in ("support_chat_sessions", "support_chat_messages"):
            op.execute(f'ALTER TABLE public."{table_name}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'REVOKE ALL PRIVILEGES ON TABLE public."{table_name}" FROM anon, authenticated')


def downgrade() -> None:
    op.drop_index("ix_support_chat_messages_session_created", table_name="support_chat_messages")
    op.drop_table("support_chat_messages")
    op.drop_index("ix_support_chat_sessions_user_updated", table_name="support_chat_sessions")
    op.drop_table("support_chat_sessions")
