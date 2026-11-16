import pytest
from sqlalchemy import create_engine, inspect, text

from src.scripts.support_chat_schema import reconcile_context
from src.shared.config import get_settings


@pytest.fixture
def existing_context(tmp_path, monkeypatch):
    database = tmp_path / "existing-context.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(database))
    get_settings.cache_clear()
    engine = create_engine("sqlite:///" + str(database))
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)",
            "INSERT INTO alembic_version VALUES ('0013_drop_custom_pool')",
            "CREATE TABLE support_chat_sessions (id TEXT PRIMARY KEY, state_json JSON NOT NULL DEFAULT '{}')",
            "CREATE TABLE support_chat_messages (id TEXT PRIMARY KEY, content TEXT NOT NULL, application_id VARCHAR(100))",
            "CREATE TABLE support_chat_rate_limits (user_id VARCHAR(100) NOT NULL PRIMARY KEY, window_started_at DATETIME NOT NULL, request_count INTEGER NOT NULL)",
            "CREATE TABLE usage_events (application_id TEXT, created_at DATETIME)",
            "INSERT INTO support_chat_messages VALUES ('existing', 'Existing reply', 'owned-app')",
        ):
            connection.execute(text(statement))
    try:
        yield engine
    finally:
        engine.dispose()
        get_settings.cache_clear()


def revision(engine):
    with engine.connect() as connection:
        return list(connection.scalars(text("SELECT version_num FROM alembic_version")))


def test_dry_run_checks_contract_without_writing(existing_context):
    report = reconcile_context()
    assert report["ready"] is True
    assert report["issues"] == []
    assert revision(existing_context) == ["0013_drop_custom_pool"]
    assert "data_json" not in {item["name"] for item in inspect(existing_context).get_columns("support_chat_messages")}


def test_apply_merges_branches_preserves_history_and_is_idempotent(existing_context):
    report = reconcile_context(apply=True)
    assert report["applied"] is True
    assert revision(existing_context) == ["20261008_support_analytics"]
    with existing_context.connect() as connection:
        assert connection.execute(
            text("SELECT content, application_id, data_json FROM support_chat_messages")
        ).one() == (
            "Existing reply",
            "owned-app",
            None,
        )
    assert any(
        item["name"] == "ix_usage_events_app_created" and item["column_names"] == ["application_id", "created_at"]
        for item in inspect(existing_context).get_indexes("usage_events")
    )
    assert reconcile_context(apply=True)["already_current"] is True


@pytest.mark.parametrize("damage", ["context", "revision", "partial", "index"])
def test_drift_blocks_before_recording_revision(existing_context, damage):
    with existing_context.begin() as connection:
        if damage == "context":
            connection.execute(text("DROP TABLE support_chat_rate_limits"))
        elif damage == "revision":
            connection.execute(text("UPDATE alembic_version SET version_num = '0011_gateway_direct_comparisons'"))
        elif damage == "partial":
            connection.execute(text("ALTER TABLE support_chat_messages ADD COLUMN data_json JSON"))
        else:
            connection.execute(text("CREATE INDEX ix_usage_events_app_created ON usage_events (created_at)"))
    before = revision(existing_context)
    assert reconcile_context()["ready"] is False
    with pytest.raises(RuntimeError, match="Reconciliation blocked"):
        reconcile_context(apply=True)
    assert revision(existing_context) == before
