from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.models.audit import AuditEventRecord
from src.shared.models.core import Application
from src.shared.models.integration import ApplicationApiKeyRecord
from tests.service_app import app


@pytest_asyncio.fixture
async def key_client(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "keys.db"))
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
async def test_application_key_create_is_one_time_and_storage_is_hash_only(key_client):
    client, _, factory = key_client
    created = await client.post("/api/v1/applications/app-keys/api-keys", json={"name": "Playground client"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["raw_key"].startswith("ak_")
    assert body["key_prefix"] == body["raw_key"][:12]
    assert "key_hash" not in body

    listed = await client.get("/api/v1/applications/app-keys/api-keys")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert "raw_key" not in listed.json()["items"][0]
    assert "key_hash" not in listed.json()["items"][0]
    assert listed.json()["items"][0]["key_prefix"] == body["key_prefix"]

    async with factory() as session:
        record = await session.scalar(select(ApplicationApiKeyRecord))
        audit = list(
            await session.scalars(
                select(AuditEventRecord).where(AuditEventRecord.resource_type == "application_api_key")
            )
        )
    assert record is not None
    assert body["raw_key"] not in record.key_hash
    assert len(record.key_hash) == 64
    assert [event.action for event in audit] == ["application_api_key.created"]


@pytest.mark.asyncio
async def test_application_key_revoke_is_idempotent_and_requires_mutation_permission(key_client):
    client, permissions, factory = key_client
    created = await client.post("/api/v1/applications/app-keys/api-keys", json={"name": "Revoke me"})
    key_id = created.json()["id"]
    first = await client.post(f"/api/v1/applications/app-keys/api-keys/{key_id}/revoke")
    second = await client.post(f"/api/v1/applications/app-keys/api-keys/{key_id}/revoke")
    assert first.status_code == second.status_code == 200
    assert first.json()["revoked_at"] == second.json()["revoked_at"]
    assert second.json()["revoked"] is True

    async with factory() as session:
        audit = list(
            await session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.resource_type == "application_api_key",
                    AuditEventRecord.action == "application_api_key.revoked",
                )
            )
        )
    assert len(audit) == 1

    permissions.clear()
    denied_create = await client.post("/api/v1/applications/app-keys/api-keys", json={"name": "Denied"})
    denied_list = await client.get("/api/v1/applications/app-keys/api-keys")
    assert denied_create.status_code == denied_list.status_code == 403
