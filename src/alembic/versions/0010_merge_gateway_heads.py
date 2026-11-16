"""Merge independent support history and gateway catalog migration branches."""
revision = "0010_merge_gateway_heads"
down_revision = ("0009_user_model_pools", "0008_support_chat_history")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
