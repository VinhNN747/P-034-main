"""Retain history when Applications are permanently deleted.

Revision ID: 20261010_application_deletion
Revises: 20261010_provider_credentials
"""
import sqlalchemy as sa

from alembic import op

revision = "20261010_application_deletion"
down_revision = "20261010_provider_credentials"
branch_labels = None
depends_on = None


_HISTORICAL_APPLICATION_TABLES = (
    "gateway_requests",
    "usage_events",
    "comparison_runs",
    "incidents",
    "policy_reviews",
)
_FK_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def _application_fk(table_name: str) -> dict:
    foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys(table_name)
    for foreign_key in foreign_keys:
        if (
            foreign_key.get("constrained_columns") == ["application_id"]
            and foreign_key.get("referred_table") == "applications"
        ):
            return foreign_key
    raise RuntimeError(f"Expected Application foreign key is missing from {table_name}")


def _drop_application_fk(table_name: str) -> None:
    foreign_key = _application_fk(table_name)
    constraint_name = op.f(
        foreign_key.get("name") or f"fk_{table_name}_application_id_applications"
    )
    with op.batch_alter_table(table_name, naming_convention=_FK_NAMING_CONVENTION) as batch_op:
        batch_op.drop_constraint(constraint_name, type_="foreignkey")


def _restore_application_fk(table_name: str) -> None:
    with op.batch_alter_table(table_name, naming_convention=_FK_NAMING_CONVENTION) as batch_op:
        batch_op.create_foreign_key(
            op.f(f"fk_{table_name}_application_id_applications"),
            "applications",
            ["application_id"],
            ["id"],
        )


def upgrade() -> None:
    op.add_column(
        "gateway_file_deletion_outbox",
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "deleted_applications",
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("application_id"),
    )
    op.create_index(
        "ix_deleted_applications_owner_user_id",
        "deleted_applications",
        ["owner_user_id"],
    )
    for table_name in _HISTORICAL_APPLICATION_TABLES:
        _drop_application_fk(table_name)


def downgrade() -> None:
    bind = op.get_bind()
    tombstone_count = int(bind.scalar(sa.text("SELECT count(*) FROM deleted_applications")) or 0)
    if tombstone_count:
        raise RuntimeError(
            "Cannot downgrade Application deletion while deleted_applications contains rows; "
            "retained history cannot be reattached to deleted Applications."
        )
    for table_name in _HISTORICAL_APPLICATION_TABLES:
        _restore_application_fk(table_name)
    op.drop_index("ix_deleted_applications_owner_user_id", table_name="deleted_applications")
    op.drop_table("deleted_applications")
    op.drop_column("gateway_file_deletion_outbox", "available_at")
