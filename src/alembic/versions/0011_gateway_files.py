"""Add gateway_files for the OpenAI-compatible Files API.

Revision ID: 0011_gateway_files
Revises: 0010_merge_gateway_heads
"""

import sqlalchemy as sa

from alembic import op

revision = "0011_gateway_files"
down_revision = "0010_merge_gateway_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gateway_files",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_backend", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_gateway_files_application_created",
        "gateway_files",
        ["application_id", "created_at"],
    )
    op.create_index("ix_gateway_files_expires_at", "gateway_files", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_gateway_files_expires_at", table_name="gateway_files")
    op.drop_index("ix_gateway_files_application_created", table_name="gateway_files")
    op.drop_table("gateway_files")
