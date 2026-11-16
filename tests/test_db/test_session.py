from pathlib import Path

import pytest
from sqlalchemy import text

from src.shared.config import get_settings
from src.shared.database.session import (
    create_engine_from_settings,
    create_session_factory,
    normalize_database_url,
)


@pytest.mark.asyncio
async def test_temporary_sqlite_url_creates_async_session(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    engine = create_engine_from_settings(database_url)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1

    await engine.dispose()


def test_domain_session_factory_reads_shared_supabase_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://supabase.example/auth")
    get_settings.cache_clear()

    engine = create_engine_from_settings()

    assert engine.url.drivername == "postgresql+psycopg"
    assert engine.url.host == "supabase.example"

    get_settings.cache_clear()


def test_bare_postgres_url_uses_async_psycopg_driver():
    assert (
        normalize_database_url("postgresql://user:password@example.com/db")
        == "postgresql+psycopg://user:password@example.com/db"
    )
