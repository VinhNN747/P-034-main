"""Optional owner-scoped unordered model pools.

Revision ID: 0009_user_model_pools
Revises: 0008_gateway_runtime_sdk
"""
from datetime import UTC
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "0009_user_model_pools"
down_revision = "0008_gateway_runtime_sdk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column("model_pools", sa.Column("owner_user_id", sa.String(36), nullable=True))
    with op.batch_alter_table("model_pools", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}) as batch:
        # PostgreSQL names the original unnamed unique constraint automatically.
        inspector = sa.inspect(bind)
        for constraint in inspector.get_unique_constraints("model_pools"):
            if constraint["column_names"] == ["name"]:
                batch.drop_constraint(constraint["name"] or "uq_model_pools_name", type_="unique")
        batch.create_unique_constraint("uq_model_pools_owner_name", ["owner_user_id", "name"])
        batch.create_index("ix_model_pools_owner_user_id", ["owner_user_id"])
    with op.batch_alter_table("application_gateway_configs") as batch:
        batch.alter_column("model_pool_id", existing_type=sa.String(36), nullable=True)
        batch.alter_column("routing_policy_id", existing_type=sa.String(36), nullable=True)

    # Preserve previously configured scopes. A shared pool becomes one private
    # copy per owner, reused by that owner's applications; no models are deleted.
    pools = bind.execute(sa.text("SELECT * FROM model_pools")).mappings().all()
    for pool in pools:
        owners = bind.execute(sa.text(
            "SELECT DISTINCT a.owner_user_id FROM applications a "
            "JOIN application_gateway_configs c ON c.application_id=a.id "
            "WHERE c.model_pool_id=:pool_id"
        ), {"pool_id": pool["id"]}).scalars().all()
        members = bind.execute(sa.text(
            "SELECT model_id FROM model_pool_members WHERE pool_id=:pool_id AND enabled=true"
        ), {"pool_id": pool["id"]}).scalars().all()
        for owner in owners:
            new_id = str(uuid4())
            bind.execute(sa.text(
                "INSERT INTO model_pools (id,name,purpose,status,routing_strategy,created_at,updated_at,owner_user_id) "
                "VALUES (:id,:name,:purpose,:status,:strategy,:created,:updated,:owner)"
            ), dict(id=new_id,name=pool["name"],purpose=pool["purpose"],status=pool["status"],
                    strategy=pool["routing_strategy"],created=pool["created_at"],updated=pool["updated_at"],owner=owner))
            for priority, model_id in enumerate(members, 1):
                bind.execute(sa.text(
                    "INSERT INTO model_pool_members (id,pool_id,model_id,priority,enabled) "
                    "VALUES (:id,:pool,:model,:priority,true)"
                ), dict(id=str(uuid4()),pool=new_id,model=model_id,priority=priority))
            bind.execute(sa.text(
                "UPDATE application_gateway_configs SET model_pool_id=:new_id, version=version+1 "
                "WHERE model_pool_id=:old_id AND application_id IN "
                "(SELECT id FROM applications WHERE owner_user_id=:owner)"
            ), dict(new_id=new_id,old_id=pool["id"],owner=owner))
    bind.execute(sa.text("UPDATE model_pools SET status='inactive' WHERE owner_user_id IS NULL"))
    with op.batch_alter_table("model_pool_members") as batch:
        batch.drop_constraint("uq_pool_priority", type_="unique")
        batch.drop_column("priority")
        batch.drop_column("enabled")
    with op.batch_alter_table("model_pools") as batch:
        batch.drop_column("routing_strategy")
    with op.batch_alter_table("routing_policies", naming_convention={"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}) as batch:
        fks = sa.inspect(bind).get_foreign_keys("routing_policies")
        for fk in fks:
            if fk["constrained_columns"] == ["pool_id"]:
                batch.drop_constraint(fk["name"] or "fk_routing_policies_pool_id_model_pools", type_="foreignkey")
        batch.drop_column("pool_id")
    if bind.dialect.name == "postgresql":
        # Supabase RBAC is external to the SQLAlchemy metadata. Use role codes,
        # never UUID literals, and tolerate installations with the earlier codes.
        for code, action in [("model_pools.read_own", "read_own"), ("model_pools.manage_own", "manage_own")]:
            bind.execute(sa.text(
                "INSERT INTO public.permissions (code,resource,action,description) "
                "VALUES (:code,'model_pools',:action,'Access own optional model pools') "
                "ON CONFLICT (code) DO NOTHING"
            ), dict(code=code,action=action))
        bind.execute(sa.text(
            "DELETE FROM public.role_permissions WHERE permission_id IN "
            "(SELECT id FROM public.permissions WHERE code IN ('model_pools.read','model_pools.manage'))"
        ))
        bind.execute(sa.text(
            "DELETE FROM public.role_permissions WHERE role_id IN "
            "(SELECT id FROM public.roles WHERE code IN ('admin','platform_admin')) "
            "AND permission_id IN (SELECT id FROM public.permissions WHERE code IN "
            "('model_pools.read_own','model_pools.manage_own'))"
        ))
        bind.execute(sa.text(
            "INSERT INTO public.role_permissions (role_id,permission_id) "
            "SELECT r.id,p.id FROM public.roles r CROSS JOIN public.permissions p "
            "WHERE r.code IN ('user','ai_engineer') AND p.code IN "
            "('model_pools.read_own','model_pools.manage_own') ON CONFLICT DO NOTHING"
        ))


def downgrade() -> None:
    # Restore the old structural contract while retaining all user-created
    # records. The original member priorities are only recoverable from backup.
    bind = op.get_bind()
    with op.batch_alter_table("model_pools") as batch:
        batch.add_column(sa.Column("routing_strategy", sa.String(50), nullable=False, server_default="priority"))
    with op.batch_alter_table("model_pool_members") as batch:
        batch.add_column(sa.Column("priority", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    rows = bind.execute(sa.text("SELECT id,pool_id FROM model_pool_members ORDER BY pool_id,model_id")).all()
    priorities = {}
    for member_id, pool_id in rows:
        priorities[pool_id] = priorities.get(pool_id, 0) + 1
        bind.execute(sa.text("UPDATE model_pool_members SET priority=:priority WHERE id=:id"),
                     dict(priority=priorities[pool_id], id=member_id))
    with op.batch_alter_table("model_pool_members") as batch:
        batch.alter_column("priority", existing_type=sa.Integer(), nullable=False)
        batch.create_unique_constraint("uq_pool_priority", ["pool_id", "priority"])
    with op.batch_alter_table("routing_policies") as batch:
        batch.add_column(sa.Column("pool_id", sa.String(36), nullable=True))
        batch.create_foreign_key("routing_policies_pool_id_fkey", "model_pools", ["pool_id"], ["id"])

    # No-pool configurations need an equivalent all-catalog pool for old code.
    from datetime import datetime
    pool_id, policy_id = str(uuid4()), str(uuid4())
    now = datetime.now(UTC)
    bind.execute(sa.text(
        "INSERT INTO model_pools (id,name,purpose,status,routing_strategy,created_at,updated_at) "
        "VALUES (:id,:name,'All models','active','priority',:now,:now)"
    ), dict(id=pool_id, name="catalog-default-" + pool_id, now=now))
    for priority, model_id in enumerate(bind.execute(sa.text("SELECT id FROM models ORDER BY id")).scalars(), 1):
        bind.execute(sa.text(
            "INSERT INTO model_pool_members (id,pool_id,model_id,priority,enabled) "
            "VALUES (:id,:pool,:model,:priority,true)"
        ), dict(id=str(uuid4()), pool=pool_id, model=model_id, priority=priority))
    bind.execute(sa.text(
        "UPDATE application_gateway_configs SET model_pool_id=:pool WHERE model_pool_id IS NULL"
    ), dict(pool=pool_id))
    bind.execute(sa.text(
        "UPDATE routing_policies SET pool_id=COALESCE((SELECT c.model_pool_id "
        "FROM application_gateway_configs c WHERE c.routing_policy_id=routing_policies.id "
        "ORDER BY c.id LIMIT 1),:pool)"
    ), dict(pool=pool_id))
    bind.execute(sa.text(
        "INSERT INTO routing_policies (id,name,pool_id,match_conditions_json,guardrails_json,"
        "fallback_chain_json,priority,status,created_at,updated_at) "
        "VALUES (:id,:name,:pool,'{}','{}','[]',100,'active',:now,:now)"
    ), dict(id=policy_id, name="catalog-default-" + policy_id, pool=pool_id, now=now))
    bind.execute(sa.text(
        "UPDATE application_gateway_configs SET routing_policy_id=:policy WHERE routing_policy_id IS NULL"
    ), dict(policy=policy_id))
    # Old routers require a policy targeting exactly each configured pool.
    # A single new policy may be shared by applications with different pools.
    pairs = bind.execute(sa.text(
        "SELECT DISTINCT c.routing_policy_id,c.model_pool_id FROM application_gateway_configs c "
        "JOIN routing_policies p ON p.id=c.routing_policy_id WHERE c.model_pool_id<>p.pool_id"
    )).all()
    for original_id, configured_pool in pairs:
        original = bind.execute(sa.text("SELECT * FROM routing_policies WHERE id=:id"), dict(id=original_id)).mappings().one()
        new_id = str(uuid4())
        # INSERT SELECT preserves JSON column types in PostgreSQL and SQLite.
        bind.execute(sa.text(
            "INSERT INTO routing_policies (id,name,pool_id,match_conditions_json,guardrails_json,"
            "fallback_chain_json,priority,status,created_at,updated_at) "
            "SELECT :new_id,:name,:pool,match_conditions_json,guardrails_json,fallback_chain_json,"
            "priority,status,created_at,updated_at FROM routing_policies WHERE id=:original"
        ), dict(new_id=new_id,name=original["name"][:60]+"-"+new_id,pool=configured_pool,original=original_id))
        bind.execute(sa.text(
            "UPDATE application_gateway_configs SET routing_policy_id=:new_id "
            "WHERE routing_policy_id=:original AND model_pool_id=:pool"
        ), dict(new_id=new_id,original=original_id,pool=configured_pool))
    with op.batch_alter_table("application_gateway_configs") as batch:
        batch.alter_column("model_pool_id", existing_type=sa.String(36), nullable=False)
        batch.alter_column("routing_policy_id", existing_type=sa.String(36), nullable=False)
    with op.batch_alter_table("routing_policies") as batch:
        batch.alter_column("pool_id", existing_type=sa.String(36), nullable=False)

    seen = set()
    for old_id, name in bind.execute(sa.text("SELECT id,name FROM model_pools ORDER BY id")).all():
        if name in seen:
            name = name[:60] + "-" + old_id
            bind.execute(sa.text("UPDATE model_pools SET name=:name WHERE id=:id"), dict(name=name, id=old_id))
        seen.add(name)
    with op.batch_alter_table("model_pools") as batch:
        batch.drop_index("ix_model_pools_owner_user_id")
        batch.drop_constraint("uq_model_pools_owner_name", type_="unique")
        batch.drop_column("owner_user_id")
        batch.create_unique_constraint("model_pools_name_key", ["name"])
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text(
            "DELETE FROM public.role_permissions WHERE permission_id IN "
            "(SELECT id FROM public.permissions WHERE code IN ('model_pools.read_own','model_pools.manage_own'))"
        ))
        bind.execute(sa.text(
            "INSERT INTO public.role_permissions (role_id,permission_id) "
            "SELECT r.id,p.id FROM public.roles r CROSS JOIN public.permissions p "
            "WHERE (r.code IN ('admin','platform_admin') AND p.code IN ('model_pools.read','model_pools.manage')) "
            "OR (r.code IN ('user','ai_engineer') AND p.code='model_pools.read') ON CONFLICT DO NOTHING"
        ))
