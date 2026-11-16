import json

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from src.shared.config import get_settings


def test_analytics_migration_preserves_history_and_adds_index(tmp_path, monkeypatch):
    database = tmp_path / "analytics-migration.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///" + str(database))
    get_settings.cache_clear()
    engine = create_engine("sqlite:///" + str(database))
    config = Config("src/alembic.ini")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE support_chat_messages (id TEXT PRIMARY KEY, content TEXT NOT NULL)"))
            connection.execute(text("CREATE TABLE usage_events (application_id TEXT, created_at DATETIME)"))
            connection.execute(text("INSERT INTO support_chat_messages VALUES ('existing', 'Existing reply')"))
        command.stamp(config, "20261007_merge_port_context")
        command.upgrade(config, "20261008_support_analytics")
        with engine.begin() as connection:
            assert connection.execute(text("SELECT content, data_json FROM support_chat_messages")).one() == (
                "Existing reply",
                None,
            )
            connection.execute(
                text("UPDATE support_chat_messages SET data_json = :data"),
                {"data": json.dumps({"result": {"total": 1}})},
            )
        assert "ix_usage_events_app_created" in {item["name"] for item in inspect(engine).get_indexes("usage_events")}
        command.downgrade(config, "20261007_merge_port_context")
        assert "data_json" not in {column["name"] for column in inspect(engine).get_columns("support_chat_messages")}
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT content FROM support_chat_messages")) == "Existing reply"
    finally:
        engine.dispose()
        get_settings.cache_clear()
