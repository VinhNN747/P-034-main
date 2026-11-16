"""Merge the Custom Pool removal and support context migration branches.

Revision ID: 20261007_merge_port_context
Revises: 0013_drop_custom_pool, 20261007_merge_context_gateway
Create Date: 2026-10-07
"""

revision = "20261007_merge_port_context"
down_revision = ("0013_drop_custom_pool", "20261007_merge_context_gateway")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
