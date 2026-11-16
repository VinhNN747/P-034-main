"""Secure Core/Gateway tables from direct Supabase Data API access.

Revision ID: 0006_secure_domain_tables
Revises: 0005_fe_integration_domain
"""

from alembic import op

revision = "0006_secure_domain_tables"
down_revision = "0005_fe_integration_domain"
branch_labels = None
depends_on = None


DOMAIN_TABLES = (
    "applications",
    "application_gateway_configs",
    "application_api_keys",
    "providers",
    "models",
    "model_pools",
    "model_pool_members",
    "routing_policies",
    "gateway_requests",
    "gateway_tasks",
    "route_decisions",
    "model_executions",
    "aggregation_results",
    "stage_traces",
    "usage_events",
    "incidents",
    "policy_reviews",
    "policy_decisions",
    "billing_accounts",
    "billing_settings",
    "billing_transactions",
    "audit_events",
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table_name in DOMAIN_TABLES:
        op.execute(f'ALTER TABLE public."{table_name}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'REVOKE ALL PRIVILEGES ON TABLE public."{table_name}" '
            "FROM anon, authenticated"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table_name in DOMAIN_TABLES:
        op.execute(
            f'GRANT ALL PRIVILEGES ON TABLE public."{table_name}" '
            "TO anon, authenticated"
        )
        op.execute(f'ALTER TABLE public."{table_name}" DISABLE ROW LEVEL SECURITY')
