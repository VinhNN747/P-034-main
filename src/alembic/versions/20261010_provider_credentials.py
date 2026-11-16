"""Add encrypted provider credentials and service bindings.

Revision ID: 20261010_provider_credentials
Revises: 20261009_merge_sdk_dev
"""

import sqlalchemy as sa

from alembic import op

revision = "20261010_provider_credentials"
down_revision = "20261009_merge_sdk_dev"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "providers",
        sa.Column("purpose", sa.String(length=20), nullable=False, server_default="target"),
    )
    with op.batch_alter_table("providers") as batch_op:
        batch_op.create_check_constraint("ck_providers_purpose", "purpose IN ('target', 'internal')")
    op.add_column(
        "models",
        sa.Column("last_probe_credential_version", sa.Integer(), nullable=True),
    )
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.String(length=36), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("encryption_version", sa.String(length=20), nullable=False, server_default="fernet-v1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("key_last4", sa.String(length=4), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("updated_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_test_status", sa.String(length=20), nullable=False, server_default="untested"),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_credential_version", sa.Integer(), nullable=True),
        sa.Column("last_test_mode", sa.String(length=20), nullable=True),
        sa.Column("last_test_model", sa.String(length=150), nullable=True),
        sa.Column("last_test_error_code", sa.String(length=80), nullable=True),
        sa.Column("last_test_latency_ms", sa.Float(), nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_provider_credentials_version_positive"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_provider_credentials_status"),
        sa.CheckConstraint(
            "last_test_status IN ('untested', 'healthy', 'failed', 'unavailable', 'skipped')",
            name="ck_provider_credentials_last_test_status",
        ),
        sa.CheckConstraint(
            "last_test_latency_ms IS NULL OR last_test_latency_ms >= 0",
            name="ck_provider_credentials_last_test_latency_nonnegative",
        ),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_id", name="uq_provider_credentials_provider"),
    )
    op.create_table(
        "service_provider_bindings",
        sa.Column("purpose", sa.String(length=40), nullable=False),
        sa.Column("provider_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "purpose IN ('internal_openai', 'ocr_google', 'routing_jev')",
            name="ck_service_provider_bindings_purpose",
        ),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("purpose"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE public."provider_credentials" ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE public."service_provider_bindings" ENABLE ROW LEVEL SECURITY')
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN "
            'REVOKE ALL PRIVILEGES ON TABLE public."provider_credentials", '
            'public."service_provider_bindings" FROM anon; END IF; END $$;'
        )
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN "
            'REVOKE ALL PRIVILEGES ON TABLE public."provider_credentials", '
            'public."service_provider_bindings" FROM authenticated; END IF; END $$;'
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN "
            'GRANT ALL PRIVILEGES ON TABLE public."provider_credentials", '
            'public."service_provider_bindings" TO anon; END IF; END $$;'
        )
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN "
            'GRANT ALL PRIVILEGES ON TABLE public."provider_credentials", '
            'public."service_provider_bindings" TO authenticated; END IF; END $$;'
        )
        op.execute('ALTER TABLE public."provider_credentials" DISABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE public."service_provider_bindings" DISABLE ROW LEVEL SECURITY')
    op.drop_table("service_provider_bindings")
    op.drop_table("provider_credentials")
    op.drop_column("models", "last_probe_credential_version")
    with op.batch_alter_table("providers") as batch_op:
        batch_op.drop_constraint("ck_providers_purpose", type_="check")
        batch_op.drop_column("purpose")
