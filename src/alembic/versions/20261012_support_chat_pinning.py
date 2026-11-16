"""Add persistent pinning for support chat sessions.

Revision ID: 20261012_support_chat_pinning
Revises: 20261010_application_deletion
"""

import sqlalchemy as sa

from alembic import op

revision = "20261012_support_chat_pinning"
down_revision = "20261010_application_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "support_chat_sessions",
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_support_chat_sessions_user_pinned_updated",
        "support_chat_sessions",
        ["user_id", "pinned", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_support_chat_sessions_user_pinned_updated",
        table_name="support_chat_sessions",
    )
    op.drop_column("support_chat_sessions", "pinned")
