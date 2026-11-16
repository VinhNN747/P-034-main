from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from src.shared.config import get_settings


def test_user_pool_migration_preserves_shared_scopes_and_defaults(tmp_path: Path, monkeypatch):
    path = tmp_path / 'pools.db'
    monkeypatch.setenv('DATABASE_URL', 'sqlite+aiosqlite:///' + str(path))
    get_settings.cache_clear()
    config = Config('src/alembic.ini')
    command.upgrade(config, '0008_gateway_runtime_sdk')
    engine = create_engine('sqlite:///' + str(path))
    with engine.begin() as c:
        c.execute(text("INSERT INTO providers (id,name,display_name,kind,status,secret_env_var,created_at,updated_at) VALUES ('p','p','P','offline','active','','2026-10-06','2026-10-06')"))
        for mid in ['a','b']:
            c.execute(text("INSERT INTO models (id,provider_id,name,display_name,status,capabilities_json,input_price_per_million,output_price_per_million,pricing_version,created_at,updated_at) VALUES (:id,'p',:id,:id,'active','[]',0,0,'1','2026-10-06','2026-10-06')"), {'id':mid})
        c.execute(text("INSERT INTO model_pools VALUES ('pool','Shared','test','active','priority','2026-10-06','2026-10-06')"))
        c.execute(text("INSERT INTO model_pool_members VALUES ('ma','pool','a',1,true),('mb','pool','b',2,false)"))
        c.execute(text("INSERT INTO routing_policies VALUES ('policy','Default','{}','pool','{}','[]',100,'active','2026-10-06','2026-10-06')"))
        for owner in ['alice','bob']:
            c.execute(text("INSERT INTO applications (id,slug,name,owner_user_id,environment,status,tags_json,created_at,updated_at) VALUES (:id,:id,:id,:id,'development','active','[]','2026-10-06','2026-10-06')"), {'id':owner})
            c.execute(text("INSERT INTO application_gateway_configs (id,application_id,model_pool_id,routing_policy_id,budget_ceiling,default_understander,default_decomposer,default_router,default_aggregator,created_at,updated_at) VALUES (:id,:id,'pool','policy',17,'rule','deterministic','deterministic','none','2026-10-06','2026-10-06')"), {'id':owner})
    command.upgrade(config, '0009_user_model_pools')
    with engine.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM model_pools WHERE owner_user_id IS NOT NULL')) == 2
        assert c.scalar(text("SELECT status FROM model_pools WHERE id='pool'")) == 'inactive'
        rows = c.execute(text('SELECT c.application_id,c.model_pool_id,c.budget_ceiling,p.owner_user_id FROM application_gateway_configs c JOIN model_pools p ON p.id=c.model_pool_id')).all()
        assert all(app == owner and budget == 17 for app, _, budget, owner in rows)
        assert len({pid for _,pid,_,_ in rows}) == 2
        assert set(c.scalars(text("SELECT model_id FROM model_pool_members m JOIN model_pools p ON p.id=m.pool_id WHERE p.owner_user_id IS NOT NULL"))) == {'a'}
    inspector = inspect(engine)
    assert 'priority' not in {c['name'] for c in inspector.get_columns('model_pool_members')}
    assert 'pool_id' not in {c['name'] for c in inspector.get_columns('routing_policies')}
    assert all(c['nullable'] for c in inspector.get_columns('application_gateway_configs') if c['name'] in {'model_pool_id','routing_policy_id'})
    command.downgrade(config, '0008_gateway_runtime_sdk')
    assert 'priority' in {c['name'] for c in inspect(engine).get_columns('model_pool_members')}
    with engine.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM application_gateway_configs c JOIN routing_policies p ON p.id=c.routing_policy_id WHERE c.model_pool_id<>p.pool_id')) == 0
    assert 'owner_user_id' not in {c['name'] for c in inspect(engine).get_columns('model_pools')}
    command.upgrade(config, '0009_user_model_pools')
    engine.dispose()
    get_settings.cache_clear()
