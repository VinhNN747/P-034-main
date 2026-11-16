from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.dtos.applications import ApplicationCreate
from src.backend.dtos.catalog import (
    ModelCreate,
    ModelPoolCreate,
    ModelPoolMemberCreate,
    PolicyCreate,
    ProviderCreate,
)
from src.backend.dtos.gateway_configuration import GatewayConfigUpsert
from src.backend.services.application_service import ApplicationService
from src.backend.services.catalog_service import CatalogService
from src.backend.services.gateway_configuration_service import GatewayConfigurationService
from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.services.gateway_runtime_service import GatewayRuntimeService
from tests.service_app import app


@pytest_asyncio.fixture
async def runtime_client(tmp_path: Path, monkeypatch):
    from src.backend_ai.routers import gateway_runtime as runtime_module

    offline_settings = Settings(
        _env_file=None,
        gateway_execution_mode="offline",
        gateway_openai_enabled=False,
        gateway_jev_enabled=False,
    )
    monkeypatch.setattr(runtime_module, "get_settings", lambda: offline_settings)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runtime-api.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as setup_session:
        application = await ApplicationService(setup_session).create(
            "user-1", ApplicationCreate(slug="api-runtime", name="API runtime")
        )
        catalog = CatalogService(setup_session)
        provider = await catalog.create_provider(ProviderCreate(name="local", display_name="Local", kind="offline"))
        model = await catalog.create_model(
            ModelCreate(
                provider_id=provider.id,
                name="local-deterministic",
                display_name="Local deterministic",
                capabilities=["chat"],
            )
        )
        pool = await catalog.create_pool(
            ModelPoolCreate(
                name="api-default",
                purpose="runtime",
                members=[ModelPoolMemberCreate(model_id=model.id)],
            ),
            "user-1",
        )
        policy = await catalog.create_policy(
            PolicyCreate(name="api-default", fallback_chain=[model.id])
        )
        await GatewayConfigurationService(setup_session).upsert(
            "user-1",
            application.id,
            GatewayConfigUpsert(model_pool_id=pool.id, routing_policy_id=policy.id),
        )

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(
            id="user-1",
            email="user@example.com",
            roles=("owner",),
            permissions=frozenset({"incidents.read", "incidents.manage", "requests.read_own"}),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, application.id
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_analyze_and_offline_runtime_request_are_available(runtime_client):
    client, application_id = runtime_client
    payload = {"application_id": application_id, "prompt": "Say hello"}

    analyzed = await client.post("/api/v1/gateway/analyze", json=payload)
    assert analyzed.status_code == 200
    assert analyzed.json()["request"]["request_id"]
    assert analyzed.json()["decomposition"] is not None
    assert analyzed.json()["execution_results"] is None

    executed = await client.post(
        "/api/v1/gateway/requests",
        json={**payload, "aggregator": "none", "router": "deterministic"},
    )
    assert executed.status_code == 200, executed.text
    body = executed.json()
    assert body["request_id"] == payload.get("request_id", body["request"]["request_id"])
    assert body["aggregation"]["status"] == "complete"
    assert body["execution_results"][0]["result"].startswith("offline:")

    request_id = body["request"]["request_id"]
    detail = await client.get(f"/api/v1/gateway/requests/{request_id}")
    traces = await client.get(f"/api/v1/gateway/requests/{request_id}/traces")
    executions = await client.get(f"/api/v1/gateway/requests/{request_id}/executions")
    assert detail.status_code == traces.status_code == executions.status_code == 200
    assert detail.json()["request_id"] == request_id
    assert len(traces.json()["items"]) >= 1
    assert len(executions.json()["items"]) == 1


@pytest.mark.asyncio
async def test_runtime_rejects_unknown_strategy_and_invalid_schema(runtime_client):
    client, application_id = runtime_client
    unknown = await client.post(
        "/api/v1/gateway/requests",
        json={"application_id": application_id, "prompt": "hello", "router": "missing"},
    )
    assert unknown.status_code == 400

    invalid = await client.post(
        "/api/v1/gateway/requests",
        json={"application_id": application_id},
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_legacy_jev_alias_routes_with_database_scope(runtime_client):
    client, application_id = runtime_client
    response = await client.post(
        "/api/v1/gateway/requests",
        json={"application_id": application_id, "prompt": "hello", "router": "jev", "aggregator": "none"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["request"]["metadata"]["router"] == "database"
    assert response.json()["model"] == "local-deterministic"


@pytest.mark.asyncio
async def test_analyze_does_not_require_execution_strategy_credentials(runtime_client):
    client, application_id = runtime_client

    response = await client.post(
        "/api/v1/gateway/analyze",
        json={
            "application_id": application_id,
            "prompt": "hello",
            "router": "jev",
            "aggregator": "llm",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["execution_results"] is None


@pytest.mark.asyncio
async def test_runtime_returns_422_when_database_router_has_no_eligible_candidate(runtime_client):
    _, application_id = runtime_client
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/gateway/requests",
            json={
                "application_id": application_id,
                "prompt": "Write Python code for this task",
                "router": "database",
                "aggregator": "none",
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "No routing candidate satisfies capabilities and constraints"


@pytest.mark.asyncio
async def test_legacy_default_router_uses_database_candidate_scope(runtime_client):
    client, application_id = runtime_client
    response = await client.post('/api/v1/gateway/requests', json={
        'application_id':application_id, 'prompt':'Say hello', 'aggregator':'none',
    })
    assert response.status_code == 200, response.text
    assert response.json()['request']['metadata']['router'] == 'database'
    assert response.json()['model'] == 'local-deterministic'


@pytest.mark.asyncio
async def test_legacy_router_enforces_pool_and_dynamic_default_catalog(runtime_client):
    from src.shared.models.gateway import ModelPool
    client, application_id = runtime_client
    db_factory = app.dependency_overrides[get_db]
    async for session in db_factory():
        config = await GatewayRuntimeService(session).config(application_id)
        pool_id = config.model_pool_id
        catalog = CatalogService(session)
        providers = await catalog.list_providers()
        extra = await catalog.create_model(ModelCreate(provider_id=providers[0].id,name='new-model',display_name='New model',capabilities=['chat']))
        break
    payload = {'application_id':application_id, 'prompt':'Say hello','aggregator':'none','router':'deterministic','constraints':{'allowed_models':[extra.id]}}
    blocked = await client.post('/api/v1/gateway/requests',json=payload)
    assert blocked.status_code == 422, blocked.text
    async for session in db_factory():
        await GatewayConfigurationService(session).upsert('user-1',application_id,GatewayConfigUpsert(model_pool_id=None))
        break
    allowed = await client.post('/api/v1/gateway/requests',json=payload)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()['model'] == 'new-model'
    async for session in db_factory():
        await GatewayConfigurationService(session).upsert('user-1',application_id,GatewayConfigUpsert(model_pool_id=pool_id))
        pool = await session.get(ModelPool,pool_id)
        pool.status='inactive'
        await session.commit()
        break
    inactive = await client.post('/api/v1/gateway/requests',json={'application_id':application_id,'prompt':'Say hello','aggregator':'none','router':'deterministic'})
    assert inactive.status_code == 422, inactive.text
