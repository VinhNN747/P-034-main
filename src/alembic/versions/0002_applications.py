"""Create Core Backend applications table.

Revision ID: 0002_applications
Revises: 0001_initial
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_applications"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("owner_user_id", sa.String(length=100), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("tags_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_applications_slug"),
    )
    op.create_index(
        "ix_applications_owner_user_id", "applications", ["owner_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_applications_owner_user_id", table_name="applications")
    op.drop_table("applications")
