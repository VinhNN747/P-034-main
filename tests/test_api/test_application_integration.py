from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.models.gateway import (
    Model,
    ModelPool,
    ModelPoolMember,
    Provider,
    RoutingPolicy,
)
from tests.service_app import app


@pytest_asyncio.fixture
async def integration_client(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "api.db"))
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
        yield client, factory
    app.dependency_overrides.clear()
    await engine.dispose()


async def seed_catalog(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        provider = Provider(
            id=str(uuid4()),
            name="offline",
            display_name="Offline",
            kind="offline",
            secret_env_var="",
        )
        model = Model(
            id=str(uuid4()),
            provider_id=provider.id,
            name="local-deterministic",
            display_name="Local deterministic",
            capabilities_json=["chat"],
        )
        pool = ModelPool(
            id=str(uuid4()),
            name="general",
            purpose="General purpose",
            owner_user_id="user-1",
        )
        member = ModelPoolMember(
            id=str(uuid4()),
            pool_id=pool.id,
            model_id=model.id,

        )
        policy = RoutingPolicy(
            id=str(uuid4()),
            name="general",
            match_conditions_json={},
            guardrails_json={},
            fallback_chain_json=[],
        )
        session.add_all([provider, model, pool, member, policy])
        await session.commit()


@pytest.mark.asyncio
async def test_application_list_filters_search_environment_and_status(
    integration_client,
) -> None:
    client, _ = integration_client
    await client.post(
        "/api/v1/applications",
        json={
            "slug": "production-one",
            "name": "Production One",
            "environment": "production",
            "status": "active",
        },
    )
    await client.post(
        "/api/v1/applications",
        json={
            "slug": "development-two",
            "name": "Development Two",
            "environment": "development",
            "status": "paused",
        },
    )

    response = await client.get(
        "/api/v1/applications",
        params={
            "search": "production",
            "environment": "production",
            "status": "active",
        },
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["slug"] == "production-one"


@pytest.mark.asyncio
async def test_application_create_returns_initial_gateway_config(
    integration_client,
) -> None:
    client, factory = integration_client
    await seed_catalog(factory)

    response = await client.post(
        "/api/v1/applications",
        json={"slug": "configured", "name": "Configured App"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["gateway_config"]["version"] == 1
    assert payload["gateway_config"]["routing_objective"] == "balanced"


@pytest.mark.asyncio
async def test_application_detail_returns_config_summary_and_request_count(
    integration_client,
) -> None:
    client, factory = integration_client
    await seed_catalog(factory)
    created = await client.post(
        "/api/v1/applications",
        json={"slug": "detail-app", "name": "Detail App"},
    )
    application_id = created.json()["id"]

    response = await client.get("/api/v1/applications/" + application_id)

    assert response.status_code == 200
    assert response.json()["config_version"] == 1
    assert response.json()["request_count"] == 0
    assert response.json()["gateway_config"]["application_id"] == application_id


@pytest.mark.asyncio
async def test_gateway_config_uses_optimistic_version_and_returns_conflict(
    integration_client,
) -> None:
    client, factory = integration_client
    await seed_catalog(factory)
    created = await client.post(
        "/api/v1/applications",
        json={"slug": "versioned-app", "name": "Versioned App"},
    )
    application_id = created.json()["id"]

    updated = await client.put(
        "/api/v1/applications/" + application_id + "/gateway-config",
        json={
            "routing_objective": "quality_first",
            "budget_ceiling": 100,
            "expected_version": 1,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2

    stale = await client.put(
        "/api/v1/applications/" + application_id + "/gateway-config",
        json={"budget_ceiling": 50, "expected_version": 1},
    )

    assert stale.status_code == 409


@pytest.mark.asyncio
async def test_gateway_config_accepts_latency_first_and_rejects_unknown_objective(
    integration_client,
) -> None:
    client, factory = integration_client
    await seed_catalog(factory)
    created = await client.post(
        "/api/v1/applications",
        json={"slug": "latency-app", "name": "Latency App"},
    )
    application_id = created.json()["id"]

    updated = await client.put(
        "/api/v1/applications/" + application_id + "/gateway-config",
        json={
            "routing_objective": "latency_first",
            "max_latency_ms": 750,
            "expected_version": 1,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["routing_objective"] == "latency_first"
    assert updated.json()["max_latency_ms"] == 750

    invalid = await client.put(
        "/api/v1/applications/" + application_id + "/gateway-config",
        json={"routing_objective": "random_first", "expected_version": 2},
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_archive_is_idempotent_and_removes_application_from_active_list(
    integration_client,
) -> None:
    client, _ = integration_client
    created = await client.post(
        "/api/v1/applications",
        json={"slug": "archive-app", "name": "Archive App"},
    )
    application_id = created.json()["id"]

    assert (await client.delete("/api/v1/applications/" + application_id)).status_code == 204
    assert (await client.delete("/api/v1/applications/" + application_id)).status_code == 204

    active = await client.get("/api/v1/applications", params={"status": "active"})
    assert active.status_code == 200
    assert active.json()["total"] == 0
