from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.dtos.application_keys import ApplicationKeyCreate
from src.backend.services.application_key_service import ApplicationKeyService
from src.shared.database.base import Base
from src.shared.models.core import Application
from src.shared.models.integration import ApplicationApiKeyRecord


@pytest_asyncio.fixture
async def auth_store(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "auth.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            Application(
                id="app-auth",
                slug="auth-app",
                name="Auth App",
                owner_user_id="owner-1",
                environment="development",
                status="active",
            )
        )
        await session.commit()
    yield factory
    await engine.dispose()


async def _new_key(factory):
    async with factory() as session:
        created = await ApplicationKeyService(session).create(
            "owner-1", "app-auth", ApplicationKeyCreate(name="runtime")
        )
        return created.raw_key, created.id


@pytest.mark.asyncio
async def test_authenticates_real_hash_and_touches_last_used(auth_store):
    from src.backend_ai.services.application_auth_service import ApplicationKeyAuthService

    raw_key, key_id = await _new_key(auth_store)
    async with auth_store() as session:
        principal = await ApplicationKeyAuthService(session).authenticate(raw_key)
    assert (principal.application_id, principal.key_id) == ("app-auth", key_id)

    async with auth_store() as session:
        key = await session.scalar(select(ApplicationApiKeyRecord).where(ApplicationApiKeyRecord.id == key_id))
    assert key is not None and key.last_used_at is not None
    assert raw_key not in key.key_hash


@pytest.mark.asyncio
async def test_wrong_secret_is_rejected_without_touching_key(auth_store):
    from src.backend_ai.services.application_auth_service import ApplicationKeyAuthService, InvalidApplicationKeyError

    raw_key, key_id = await _new_key(auth_store)
    async with auth_store() as session:
        with pytest.raises(InvalidApplicationKeyError):
            await ApplicationKeyAuthService(session).authenticate(
                raw_key[:-1] + ("a" if raw_key[-1] != "a" else "b")
            )
    async with auth_store() as session:
        key = await session.scalar(select(ApplicationApiKeyRecord).where(ApplicationApiKeyRecord.id == key_id))
    assert key is not None and key.last_used_at is None


@pytest.mark.asyncio
async def test_revoked_key_is_rejected(auth_store):
    from src.backend_ai.services.application_auth_service import ApplicationKeyAuthService, InvalidApplicationKeyError

    raw_key, key_id = await _new_key(auth_store)
    async with auth_store() as session:
        await ApplicationKeyService(session).revoke("owner-1", "app-auth", key_id)
    async with auth_store() as session:
        with pytest.raises(InvalidApplicationKeyError):
            await ApplicationKeyAuthService(session).authenticate(raw_key)


@pytest.mark.asyncio
async def test_inactive_application_is_rejected(auth_store):
    from src.backend_ai.services.application_auth_service import ApplicationKeyAuthService, InactiveApplicationError

    raw_key, _ = await _new_key(auth_store)
    async with auth_store() as session:
        application = await session.get(Application, "app-auth")
        assert application is not None
        application.status = "suspended"
        await session.commit()
    async with auth_store() as session:
        with pytest.raises(InactiveApplicationError):
            await ApplicationKeyAuthService(session).authenticate(raw_key)
