from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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
from src.shared.models.gateway import ApplicationGatewayConfig
from tests.service_app import app


@pytest_asyncio.fixture
async def playground_client(tmp_path: Path, monkeypatch):
    from src.backend_ai.routers import gateway_runtime as runtime_module

    offline_settings = Settings(_env_file=None, gateway_execution_mode="offline")
    monkeypatch.setattr(runtime_module, "get_settings", lambda: offline_settings)
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "playground.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        application = await ApplicationService(session).create(
            "user-1", ApplicationCreate(slug="playground", name="Playground")
        )
        catalog = CatalogService(session)
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
                name="playground-default",
                purpose="playground",
                members=[ModelPoolMemberCreate(model_id=model.id)],
            ),
            "user-1",
        )
        policy = await catalog.create_policy(
            PolicyCreate(name="playground-default", fallback_chain=[model.id])
        )
        await GatewayConfigurationService(session).upsert(
            "user-1",
            application.id,
            GatewayConfigUpsert(
                model_pool_id=pool.id,
                routing_policy_id=policy.id,
                budget_ceiling=0.05
            ),
        )
        config = await session.scalar(
            select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == application.id)
        )
        config.max_output_tokens = 32
        config.timeout_seconds = 3
        config.retry_limit = 0
        await session.commit()

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(
            id="user-1",
            email="user@example.com",
            roles=("owner",),
            permissions=frozenset(),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, application.id
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_playground_returns_normalized_result_and_persisted_trace(playground_client):
    client, application_id = playground_client
    response = await client.post(
        "/api/v1/gateway/requests",
        json={
            "application_id": application_id,
            "prompt": "Say hello",
            "source": "playground",
            "router": "database",
            "aggregator": "none",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_id"]
    assert body["source"] == "playground"
    assert body["status"] == "completed"
    assert body["output"].startswith("offline:")
    assert body["provider"] == "local"
    assert body["model"] == "local-deterministic"
    assert body["trace_count"] >= 1
    assert body["total_tokens"] == body["input_tokens"] + body["output_tokens"]

    request_id = body["request_id"]
    detail = await client.get(f"/api/v1/gateway/requests/{request_id}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["output"] == body["output"]
    assert detail_body["source"] == "playground"
    assert detail_body["traces"]
    assert detail_body["executions"]
    assert detail_body["aggregation"]["status"] == "complete"


@pytest.mark.asyncio
async def test_playground_caps_client_constraints_to_server_configuration(playground_client):
    client, application_id = playground_client
    response = await client.post(
        "/api/v1/gateway/requests",
        json={
            "application_id": application_id,
            "prompt": "Say hello",
            "source": "playground",
            "constraints": {"max_output_tokens": 10000, "max_cost_usd": 1000},
            "router": "database",
            "aggregator": "none",
        },
    )

    assert response.status_code == 200, response.text
    request_id = response.json()["request_id"]
    detail = await client.get(f"/api/v1/gateway/requests/{request_id}")
    constraints = detail.json()["constraints"]
    assert constraints["max_output_tokens"] == 32
    assert constraints["max_cost_usd"] == 0.05


@pytest.mark.asyncio
async def test_playground_api_gateway_rate_limit_prompt_is_routable(playground_client):
    client, application_id = playground_client
    response = await client.post(
        "/api/v1/gateway/requests",
        json={
            "application_id": application_id,
            "prompt": "Một API Gateway có giới hạn 120 request/phút. Có 3 service gửi request với tốc độ 35, 50 và 45 request/phút. Hãy tính tổng và xác định có vượt rate limit hay không.",
            "source": "playground",
            "router": "database",
            "aggregator": "none",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["output"].startswith("offline:")


@pytest.mark.asyncio
async def test_playground_uses_the_application_understander_when_none_is_sent(playground_client):
    # The application config defaults to jev; the offline pipeline runs the classifier
    # understander instead (the rules here: no model in tests).
    client, application_id = playground_client
    response = await client.post(
        "/api/v1/gateway/requests",
        json={"application_id": application_id, "prompt": "Say hello", "source": "playground", "aggregator": "none"},
    )
    assert response.status_code == 200, response.text
    detail = await client.get(f"/api/v1/gateway/requests/{response.json()['request_id']}")
    assert "classifier->rule" in detail.text
