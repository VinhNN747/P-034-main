"""Enforce unique application names.

Revision ID: 20261012_application_name_unique
Revises: 20261012_support_chat_pinning
"""

from alembic import op

revision = "20261012_application_name_unique"
down_revision = "20261012_support_chat_pinning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A unique index works on both PostgreSQL and SQLite (the local default),
    # unlike ALTER TABLE ADD CONSTRAINT on SQLite.
    op.create_index(
        "uq_applications_name",
        "applications",
        ["name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_applications_name", table_name="applications")
