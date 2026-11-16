"""Durable blob deletion outbox.

Revision ID: 20261008_file_lifecycle
Revises: 20261008_files_quota
"""
import sqlalchemy as sa

from alembic import op

revision = '20261008_file_lifecycle'
down_revision = '20261008_files_quota'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('gateway_files', sa.Column('lifecycle', sa.String(20), nullable=False, server_default='active'))
    op.execute("UPDATE gateway_files SET lifecycle='deleted' WHERE deleted_at IS NOT NULL")
    op.create_table('gateway_file_deletion_outbox',
        sa.Column('file_id', sa.String(40), primary_key=True),
        sa.Column('storage_backend', sa.String(16), nullable=False),
        sa.Column('storage_key', sa.String(512), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('lease_owner', sa.String(36), nullable=True),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_gateway_file_deletion_outbox_lease_until', 'gateway_file_deletion_outbox', ['lease_until'])


def downgrade():
    op.drop_table('gateway_file_deletion_outbox')
    op.drop_column('gateway_files', 'lifecycle')
