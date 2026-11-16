"""Custom Pool: one model per task tier on the application gateway config.

Revision ID: 0012_custom_model_pool
Revises: c26a083f011b (merge of 0011_gateway_files and 0011_gateway_direct_comparisons)

``custom_pool_json`` holds ``{"simple": model_id, "mid": model_id, "hard":
model_id}``; null keeps the Default Pool. Existing ``model_pool_id`` values
are left untouched (still honoured by routing) so this migration loses no
configuration; choosing Default or Custom in the UI clears them.
"""

import sqlalchemy as sa

from alembic import op

revision = "0012_custom_model_pool"
down_revision = "c26a083f011b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("application_gateway_configs", sa.Column("custom_pool_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("application_gateway_configs") as batch:
        batch.drop_column("custom_pool_json")
