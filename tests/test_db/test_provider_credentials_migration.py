from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from src.shared.config import get_settings


def config_for(path: Path) -> Config:
    config = Config("src/alembic.ini")
    config.set_main_option("sqlalchemy.url", "sqlite+aiosqlite:///" + str(path))
    return config


def test_additive_migration_preserves_catalog_and_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "credential-migration.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///" + str(path))
    get_settings.cache_clear()
    config = config_for(path)
    command.upgrade(config, "20261009_merge_sdk_dev")
    engine = create_engine("sqlite:///" + str(path))
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO providers (id,name,display_name,kind,status,secret_env_var,created_at,updated_at) "
                "VALUES ('provider-1','legacy','Legacy','openai','active','OPENAI_API_KEY',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ))
            connection.execute(text(
                "INSERT INTO models (id,provider_id,name,display_name,status,capabilities_json,input_price_per_million,"
                "output_price_per_million,pricing_version,created_at,updated_at) "
                "VALUES ('model-1','provider-1','gpt-test','GPT test','active','[]',0,0,'1',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ))
            connection.execute(text(
                "INSERT INTO applications "
                "(id,slug,name,owner_user_id,environment,status,tags_json,created_at,updated_at) "
                "VALUES ('application-1','app','Application','owner','development','active','[]',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ))
            connection.execute(text(
                "INSERT INTO gateway_requests "
                "(request_id,application_id,messages_json,constraints_json,strategies_json,source,fallback_used,"
                "response_json,status,total_cost,total_latency_ms,started_at) "
                "VALUES ('request-1','application-1','[]','{}','{}','playground',0,'{}','completed',0,0,CURRENT_TIMESTAMP)"
            ))

        command.upgrade(config, "head")
        inspector = inspect(engine)
        assert {"provider_credentials", "service_provider_bindings"} <= set(inspector.get_table_names())
        assert {column["name"] for column in inspector.get_columns("providers")} >= {"purpose"}
        assert {column["name"] for column in inspector.get_columns("models")} >= {"last_probe_credential_version"}
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT name FROM providers WHERE id='provider-1'")) == "legacy"
            assert connection.scalar(text("SELECT name FROM models WHERE id='model-1'")) == "gpt-test"
            assert connection.scalar(text("SELECT request_id FROM gateway_requests WHERE request_id='request-1'")) == "request-1"
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_unique_credential_per_provider_and_downgrade_only_removes_new_schema(tmp_path: Path, monkeypatch):
    path = tmp_path / "credential-constraints.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///" + str(path))
    get_settings.cache_clear()
    config = config_for(path)
    command.upgrade(config, "head")
    engine = create_engine("sqlite:///" + str(path))
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO providers (id,name,display_name,kind,status,purpose,secret_env_var,created_at,updated_at) "
                "VALUES ('p','p','P','openai','active','target','',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ))
            values = "('c1','p','cipher','fernet-v1',1,'active','test',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'untested')"
            connection.execute(text(
                "INSERT INTO provider_credentials "
                "(id,provider_id,encrypted_api_key,encryption_version,version,status,key_last4,created_at,updated_at,last_test_status) "
                f"VALUES {values}"
            ))
            with pytest.raises(Exception):
                connection.execute(text(
                    "INSERT INTO provider_credentials "
                    "(id,provider_id,encrypted_api_key,encryption_version,version,status,key_last4,created_at,updated_at,last_test_status) "
                    "VALUES ('c2','p','cipher','fernet-v1',1,'active','test',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'untested')"
                ))

        command.downgrade(config, "20261009_merge_sdk_dev")
        inspector = inspect(engine)
        assert "provider_credentials" not in inspector.get_table_names()
        assert "service_provider_bindings" not in inspector.get_table_names()
        assert "purpose" not in {column["name"] for column in inspector.get_columns("providers")}
        assert "last_probe_credential_version" not in {
            column["name"] for column in inspector.get_columns("models")
        }
    finally:
        engine.dispose()
        get_settings.cache_clear()
