"""Store bounded support analytics snapshots for private charts.

Revision ID: 20261008_support_analytics
Revises: 20261007_merge_port_context
"""

import sqlalchemy as sa

from alembic import op

revision = "20261008_support_analytics"
down_revision = "20261007_merge_port_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("support_chat_messages", sa.Column("data_json", sa.JSON(), nullable=True))
    op.create_index("ix_usage_events_app_created", "usage_events", ["application_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_usage_events_app_created", table_name="usage_events")
    op.drop_column("support_chat_messages", "data_json")
