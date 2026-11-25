from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.models.core import Application
from src.shared.models.integration import ApplicationApiKeyRecord
from tests.service_app import app


@pytest_asyncio.fixture
async def description_key_client(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "key_descriptions.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    permissions: set[str] = {"applications.read_own", "applications.manage_own"}
    async with factory() as session:
        session.add(
            Application(
                id="app-keys",
                slug="keys-app",
                name="Keys App",
                owner_user_id="user-1",
                environment="development",
                status="active",
            )
        )
        await session.commit()

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(
            id="user-1",
            email="user@example.com",
            roles=("owner",),
            permissions=frozenset(permissions),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, permissions, factory
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_api_key_accepts_optional_description(description_key_client):
    client, _, factory = description_key_client

    created = await client.post(
        "/api/v1/applications/app-keys/api-keys",
        json={"name": "Described key", "description": "Used by the staging integration tests"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["description"] == "Used by the staging integration tests"

    async with factory() as session:
        record = await session.scalar(select(ApplicationApiKeyRecord))
    assert record is not None
    assert record.description == "Used by the staging integration tests"


@pytest.mark.asyncio
async def test_create_api_key_without_description_is_allowed(description_key_client):
    client, _, factory = description_key_client

    created = await client.post(
        "/api/v1/applications/app-keys/api-keys",
        json={"name": "Key without description"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["description"] in (None, "")

    async with factory() as session:
        record = await session.scalar(select(ApplicationApiKeyRecord))
    assert record is not None
    assert record.description in (None, "")


@pytest.mark.asyncio
async def test_api_key_list_exposes_description_for_new_and_old_keys(description_key_client):
    client, _, factory = description_key_client

    async with factory() as session:
        old_record = ApplicationApiKeyRecord(
            application_id="app-keys",
            name="Legacy key",
            key_prefix="ak_legacy_01",
            key_hash="0" * 64,
        )
        session.add(old_record)
        await session.commit()

    created = await client.post(
        "/api/v1/applications/app-keys/api-keys",
        json={"name": "New key", "description": "New key description"},
    )
    assert created.status_code == 201, created.text

    listed = await client.get("/api/v1/applications/app-keys/api-keys")
    assert listed.status_code == 200, listed.text
    items = {item["name"]: item for item in listed.json()["items"]}

    assert items["Legacy key"]["description"] in (None, "")
    assert items["New key"]["description"] == "New key description"
