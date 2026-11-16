"""Merge gateway files and direct comparisons heads

Revision ID: c26a083f011b
Revises: 0011_gateway_direct_comparisons, 0011_gateway_files
Create Date: 2026-10-06 23:14:14.701869
"""
from collections.abc import Sequence

revision: str = 'c26a083f011b'
down_revision: str | None = ('0011_gateway_direct_comparisons', '0011_gateway_files')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
