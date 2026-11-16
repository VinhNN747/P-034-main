"""Store validated model limits and feature metadata in the catalog."""

import sqlalchemy as sa

from alembic import op

revision = "20261017_anthropic_features"
down_revision = "20261015_offline_pipelines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("models", sa.Column("max_output_tokens", sa.Integer(), nullable=True))
    op.add_column("models", sa.Column("feature_metadata_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("models") as batch_op:
        batch_op.drop_column("feature_metadata_json")
        batch_op.drop_column("max_output_tokens")
