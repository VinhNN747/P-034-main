"""Remove the Custom Pool feature: drop application_gateway_configs.custom_pool_json.

Revision ID: 0013_drop_custom_pool
Revises: 0012_custom_model_pool

The column added by 0012 is no longer read or written. Downgrade re-creates
it empty (NULL means the Default Pool), so no saved pool comes back.
"""

import sqlalchemy as sa

from alembic import op

revision = "0013_drop_custom_pool"
down_revision = "0012_custom_model_pool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("application_gateway_configs") as batch:
        batch.drop_column("custom_pool_json")


def downgrade() -> None:
    op.add_column("application_gateway_configs", sa.Column("custom_pool_json", sa.JSON(), nullable=True))
