from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from src.shared.config import get_settings


def test_context_migration_preserves_existing_sessions_and_messages(tmp_path, monkeypatch):
    path = tmp_path / "support-migration.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///" + str(path))
    get_settings.cache_clear()
    engine = create_engine("sqlite:///" + str(path))
    config = Config("src/alembic.ini")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE support_chat_sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL)"))
            connection.execute(text("CREATE TABLE support_chat_messages (id TEXT PRIMARY KEY, content TEXT NOT NULL)"))
            connection.execute(text("INSERT INTO support_chat_sessions VALUES ('session', 'Existing title')"))
            connection.execute(text("INSERT INTO support_chat_messages VALUES ('message', 'Existing message')"))
        command.stamp(config, "0011_gateway_direct_comparisons")
        command.upgrade(config, "0012_support_chat_context")
        with engine.connect() as connection:
            row = connection.execute(text("SELECT title, state_json FROM support_chat_sessions")).one()
            assert row == ("Existing title", "{}")
            assert connection.execute(text("SELECT content, application_id FROM support_chat_messages")).one() == (
                "Existing message",
                None,
            )
        command.downgrade(config, "0011_gateway_direct_comparisons")
        assert "state_json" not in {column["name"] for column in inspect(engine).get_columns("support_chat_sessions")}
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT content FROM support_chat_messages")) == "Existing message"
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_isolated_upgrade_preserves_external_revision_and_rejects_reapplication(tmp_path, monkeypatch):
    import pytest

    from src.scripts.support_chat_schema import inspect_schema, isolated_upgrade

    path = tmp_path / "external-support.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(path))
    get_settings.cache_clear()
    engine = create_engine("sqlite:///" + str(path))
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('external_revision')"))
            connection.execute(text("CREATE TABLE applications (id TEXT, owner_user_id TEXT)"))
            connection.execute(
                text(
                    "CREATE TABLE support_chat_sessions (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, created_at TEXT, updated_at TEXT)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE support_chat_messages (id TEXT PRIMARY KEY, session_id TEXT, user_id TEXT, role TEXT, content TEXT, status TEXT, required_permission TEXT, sources_json JSON, actions_json JSON, created_at TEXT)"
                )
            )
            connection.execute(text("INSERT INTO support_chat_sessions (id,title) VALUES ('s','Existing')"))
        with pytest.raises(RuntimeError, match="revision changed"):
            isolated_upgrade("wrong_revision")
        assert not inspect_schema()["state_column"]
        isolated_upgrade("external_revision")
        state = inspect_schema()
        assert state["revisions"] == ["external_revision"]
        assert all(state[key] for key in ("state_column", "scope_column", "rate_limit_table"))
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT title FROM support_chat_sessions")) == "Existing"
        with pytest.raises(RuntimeError, match="previously applied"):
            isolated_upgrade("external_revision")
    finally:
        engine.dispose()
        get_settings.cache_clear()
