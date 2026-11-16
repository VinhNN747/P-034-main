"""Atomic application file reservations.

Revision ID: 20261008_files_quota
Revises: 20261007_merge_port_context
"""
import sqlalchemy as sa

from alembic import op

revision = '20261008_files_quota'
down_revision = '20261007_merge_port_context'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('gateway_file_reservations',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('application_id', sa.String(36), sa.ForeignKey('applications.id'), nullable=False),
        sa.Column('bytes', sa.BigInteger(), nullable=False),
        sa.Column('storage_backend', sa.String(16), nullable=False, server_default='local'),
        sa.Column('state', sa.String(16), nullable=False, server_default='writing'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('file_id', sa.String(40), nullable=True))
    op.create_index('ix_gateway_file_reservations_application_id', 'gateway_file_reservations', ['application_id'])
    op.create_index('ix_gateway_file_reservations_expires_at', 'gateway_file_reservations', ['expires_at'])


def downgrade():
    op.drop_table('gateway_file_reservations')
