from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend_ai.gateway.aggregation.no_aggregator import NoAggregator
from src.backend_ai.gateway.pipeline.orchestrator import GatewayOrchestrator
from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.routers.gateway_runtime import ApplicationDatabaseRouter
from src.backend_ai.services.gateway_routing_service import DatabaseRoutingService
from src.scripts.seed_backend import seed_session
from src.shared.database.base import Base
from src.shared.dtos.gateway import GatewayRequest
from src.shared.models.core import Application
from src.shared.models.gateway import ApplicationGatewayConfig, Model, ModelPool, ModelPoolMember, Provider
from src.shared.services.gateway_persistence_service import GatewayPersistenceService


@pytest_asyncio.fixture
async def seed_session_fixture(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'seed.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_seeded_offline_request_completes(seed_session_fixture):
    first = await seed_session(seed_session_fixture)
    second = await seed_session(seed_session_fixture)

    assert first == second
    assert second.providers == 3
    assert second.models == 6
    assert second.pools == 0
    assert second.policies == 3
    assert second.applications == 3
    assert second.configurations == 1

    application = await seed_session_fixture.scalar(
        select(Application).where(Application.slug == "property-intelligence")
    )
    config = await seed_session_fixture.scalar(
        select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == application.id)
    )
    assert config is not None

    router = ApplicationDatabaseRouter(
        DatabaseRoutingService(seed_session_fixture, application.id),
        GatewayRequest(request_id="constraints", application_id=application.id, prompt="x").constraints,
    )
    orchestrator = GatewayOrchestrator(
        RequestPipeline(),
        routers={"database": router},
        aggregators={"none": NoAggregator()},
        persistence=GatewayPersistenceService(seed_session_fixture),
    )
    result = await orchestrator.run(
        GatewayRequest(request_id="seed-smoke", application_id=application.id, prompt="hello"),
        router="database",
        aggregator="none",
    )

    assert result.aggregation.status == "complete"
    assert result.execution_results[0].result.startswith("offline:")


@pytest.mark.asyncio
async def test_seed_preserves_database_managed_models_and_custom_pools(seed_session_fixture):
    await seed_session(seed_session_fixture)
    openai = await seed_session_fixture.scalar(select(Provider).where(Provider.kind == "openai"))
    custom_model = Model(
        provider_id=openai.id,
        name="database-managed-model",
        display_name="Database managed model",
        capabilities_json=["chat", "reasoning"],
        target_latency_ms=450,
        input_price_per_million=0.3,
        output_price_per_million=0.8,
        pricing_version="operator-managed",
        quality_tier="balanced",
    )
    custom_pool = ModelPool(name="database-managed-pool", purpose="custom", owner_user_id="owner-1")
    seed_session_fixture.add_all([custom_model, custom_pool])
    await seed_session_fixture.flush()
    membership = ModelPoolMember(
        pool_id=custom_pool.id,
        model_id=custom_model.id,

    )
    seed_session_fixture.add(membership)
    await seed_session_fixture.commit()

    await seed_session(seed_session_fixture)
    await seed_session_fixture.refresh(custom_model)
    await seed_session_fixture.refresh(membership)

    assert custom_model.status == "active"
    assert list(
        await seed_session_fixture.scalars(
            select(ModelPoolMember.model_id).where(ModelPoolMember.pool_id == custom_pool.id)
        )
    ) == [custom_model.id]
