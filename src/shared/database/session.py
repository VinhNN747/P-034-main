from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.shared.config import Settings, get_settings


def normalize_database_url(database_url: str) -> str:
    """Select an async SQLAlchemy driver for supported bare database URLs."""
    if database_url.startswith("sqlite:///"):
        return "sqlite+aiosqlite:///" + database_url.removeprefix("sqlite:///")
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix(
            "postgresql://"
        )
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    return database_url


def create_engine_from_settings(
    database_url: str | None = None,
    settings: Settings | None = None,
) -> AsyncEngine:
    selected_url = database_url or (settings or get_settings()).database_url
    normalized_url = normalize_database_url(selected_url)
    engine_options: dict[str, object] = {"pool_pre_ping": True}
    if normalized_url.startswith("postgresql+"):
        # Supabase session-mode pooler has a small per-project session limit.
        # Keep this process bounded; deployment-wide capacity is shared by all
        # workers, so do not let SQLAlchemy create overflow connections.
        engine_options.update(
            pool_size=10,
            max_overflow=0,
            pool_timeout=10,
            pool_recycle=1800,
        )
    return create_async_engine(
        normalized_url,
        **engine_options,
    )


def create_session_factory(
    session_engine: AsyncEngine | None = None,
) -> async_sessionmaker[AsyncSession]:
    bound_engine = session_engine or engine
    return async_sessionmaker(
        bind=bound_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


engine = create_engine_from_settings()
AsyncSessionLocal = create_session_factory(engine)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
