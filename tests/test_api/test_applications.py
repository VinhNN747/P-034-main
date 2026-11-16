from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from tests.service_app import app


@pytest_asyncio.fixture
async def application_client(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(
            id="user-1",
            email="user@example.com",
            roles=("owner",),
            permissions=frozenset({"applications.read_own", "applications.write_own"}),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_application_api_create_and_paginated_list(application_client):
    created = await application_client.post(
        "/api/v1/applications",
        json={"slug": "demo", "name": "Demo App", "tags": ["demo"]},
    )

    assert created.status_code == 201
    assert created.json()["owner_user_id"] == "user-1"

    listed = await application_client.get("/api/v1/applications", params={"page": 1, "page_size": 10})

    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["slug"] == "demo"


@pytest.mark.asyncio
async def test_application_api_accepts_multiple_sort_criteria(application_client):
    for payload in (
        {"slug": "alpha", "name": "Alpha", "environment": "production"},
        {"slug": "bravo", "name": "Bravo", "environment": "development"},
    ):
        assert (await application_client.post("/api/v1/applications", json=payload)).status_code == 201

    listed = await application_client.get(
        "/api/v1/applications",
        params={"sort_by": "environment,name", "sort_dir": "asc,desc"},
    )

    assert listed.status_code == 200
    assert [item["slug"] for item in listed.json()["items"]] == ["bravo", "alpha"]


@pytest.mark.asyncio
async def test_application_api_accepts_manage_own_permission(application_client):
    async def manage_user():
        return CurrentUser(
            id="manage-user",
            email="manage@example.com",
            roles=("ai_engineer",),
            permissions=frozenset({"applications.read_own", "applications.manage_own"}),
        )

    app.dependency_overrides[get_current_user] = manage_user
    response = await application_client.post(
        "/api/v1/applications",
        json={"slug": "manage-own", "name": "Manage Own App"},
    )

    assert response.status_code == 201
    assert response.json()["owner_user_id"] == "manage-user"


@pytest.mark.asyncio
async def test_application_api_rejects_duplicate_slug(application_client):
    payload = {"slug": "same", "name": "Same"}
    assert (await application_client.post("/api/v1/applications", json=payload)).status_code == 201

    duplicate = await application_client.post("/api/v1/applications", json=payload)

    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_application_api_rejects_duplicate_name(application_client):
    assert (
        await application_client.post(
            "/api/v1/applications",
            json={"slug": "first-name", "name": "Shared name"},
        )
    ).status_code == 201

    duplicate = await application_client.post(
        "/api/v1/applications",
        json={"slug": "second-name", "name": "Shared name"},
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Application name already exists"


@pytest.mark.asyncio
async def test_application_api_archives_application(application_client):
    created = await application_client.post("/api/v1/applications", json={"slug": "archive-me", "name": "Archive Me"})
    application_id = created.json()["id"]

    archived = await application_client.delete(f"/api/v1/applications/{application_id}")
    assert archived.status_code == 204

    invalid_update = await application_client.patch(
        f"/api/v1/applications/{application_id}",
        json={"status": "active"},
    )
    assert invalid_update.status_code == 409
