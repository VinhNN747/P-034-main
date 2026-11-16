from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from src.shared.config import get_settings


def _config(path: Path) -> Config:
    config = Config("src/alembic.ini")
    config.set_main_option("sqlalchemy.url", "sqlite+aiosqlite:///" + str(path))
    return config


def _migrate(path: Path, revision: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///" + str(path))
    get_settings.cache_clear()
    command.upgrade(_config(path), revision)
    get_settings.cache_clear()


def test_runtime_operation_migration_is_additive_and_reversible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "runtime-migration.db"
    _migrate(path, "0007_application_latency_sla", monkeypatch)
    engine = create_engine("sqlite:///" + str(path))
    application_id = str(uuid4())
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO applications "
                    "(id, slug, name, description, owner_user_id, environment, status, tags_json, created_at, updated_at) "
                    "VALUES (:id, 'pre-runtime', 'Pre runtime', NULL, 'owner', 'development', 'active', '[]', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": application_id},
            )
    finally:
        engine.dispose()

    _migrate(path, "head", monkeypatch)
    engine = create_engine("sqlite:///" + str(path))
    try:
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("gateway_requests")}
        indexes = {index["name"] for index in inspector.get_indexes("gateway_requests")}
        operations = {column["name"] for column in inspector.get_columns("runtime_operations")}
        unique = {constraint["name"] for constraint in inspector.get_unique_constraints("runtime_operations")}
        assert {"application_api_key_id", "config_version"} <= columns
        assert "ix_gateway_requests_api_key_started" in indexes
        assert {
            "request_id",
            "application_id",
            "application_api_key_id",
            "idempotency_key_hash",
            "payload_hash",
            "config_snapshot_json",
            "http_body_json",
        } <= operations
        assert "uq_runtime_operations_app_key" in unique
    finally:
        engine.dispose()

    engine = create_engine("sqlite:///" + str(path))
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT slug FROM applications WHERE id = :id"), {"id": application_id}) == "pre-runtime"
    finally:
        engine.dispose()

    get_settings.cache_clear()
    command.downgrade(_config(path), "0007_application_latency_sla")
    get_settings.cache_clear()
    engine = create_engine("sqlite:///" + str(path))
    try:
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("gateway_requests")}
        assert "runtime_operations" not in inspector.get_table_names()
        assert not {"application_api_key_id", "config_version"} & columns
    finally:
        engine.dispose()
