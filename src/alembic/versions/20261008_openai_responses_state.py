"""Public Responses retention, independent from execution receipts."""
import sqlalchemy as sa

from alembic import op

revision = '20261008_responses_state'
down_revision = '20261008_file_lifecycle'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('gateway_openai_responses',
        sa.Column('id', sa.String(80), primary_key=True),
        sa.Column('application_id', sa.String(36), sa.ForeignKey('applications.id'), nullable=False),
        sa.Column('request_id', sa.String(80), nullable=False, unique=True),
        sa.Column('response_json', sa.JSON(), nullable=True),
        sa.Column('history_json', sa.JSON(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_gateway_openai_responses_application_id', 'gateway_openai_responses', ['application_id'])
    op.create_index('ix_gateway_openai_responses_expires_at', 'gateway_openai_responses', ['expires_at'])


def downgrade():
    op.drop_table('gateway_openai_responses')
