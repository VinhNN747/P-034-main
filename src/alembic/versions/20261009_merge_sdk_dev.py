"""Converge SDK state/files and dev analytics/pool-role histories.

Both published branches are retained; this revision changes no tables or data.
"""

revision = "20261009_merge_sdk_dev"
down_revision = ("20261008_responses_state", "20261009_pool_member_role")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
