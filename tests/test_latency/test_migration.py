import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def _revision_module():
    path = Path("src/alembic/versions/20261014_adaptive_latency.py")
    spec = importlib.util.spec_from_file_location("adaptive_latency_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_latency_migration_creates_independent_tables_and_downgrades_cleanly():
    engine = create_engine("sqlite:///:memory:")
    migration = _revision_module()
    try:
        with engine.begin() as connection:
            migration_context = MigrationContext.configure(connection)
            with Operations.context(migration_context):
                migration.upgrade()
            inspector = inspect(connection)
            assert {
                "latency_observations", "latency_training_runs", "latency_model_state"
            }.issubset(set(inspector.get_table_names()))
            assert {
                "resolution_source", "resolution_reason"
            }.issubset({column["name"] for column in inspector.get_columns("latency_observations")})
            assert inspector.get_foreign_keys("latency_observations") == []
            with Operations.context(migration_context):
                migration.downgrade()
            assert not {
                "latency_observations", "latency_training_runs", "latency_model_state"
            }.intersection(inspect(connection).get_table_names())
            with Operations.context(migration_context):
                migration.upgrade()
            assert {
                "latency_observations", "latency_training_runs", "latency_model_state"
            }.issubset(set(inspect(connection).get_table_names()))
    finally:
        engine.dispose()
