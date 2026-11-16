from pathlib import Path

import pytest
import pytest_asyncio
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
from src.backend.services.catalog_service import CatalogReferenceError, CatalogService
from src.backend.services.gateway_configuration_service import GatewayConfigurationService
from src.shared.database.base import Base


@pytest_asyncio.fixture
async def gateway_session(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'configuration.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_application_gateway_config_requires_existing_references(gateway_session):
    application = await ApplicationService(gateway_session).create(
        "owner-1", ApplicationCreate(slug="demo", name="Demo")
    )
    catalog = CatalogService(gateway_session)
    provider = await catalog.create_provider(
        ProviderCreate(
            name="local",
            display_name="Local",
            kind="local",
        )
    )
    model = await catalog.create_model(
        ModelCreate(provider_id=provider.id, name="deterministic", display_name="Deterministic")
    )
    pool = await catalog.create_pool(
        ModelPoolCreate(
            name="default",
            purpose="general",
            members=[ModelPoolMemberCreate(model_id=model.id)],
        ),
        "owner-1",
    )
    policy = await catalog.create_policy(PolicyCreate(name="default", match_conditions={}))

    config = await GatewayConfigurationService(gateway_session).upsert(
        "owner-1",
        application.id,
        GatewayConfigUpsert(
            model_pool_id=pool.id,
            routing_policy_id=policy.id,
            budget_ceiling=10.0
        ),
    )

    assert config.application_id == application.id
    assert config.model_pool_id == pool.id
    assert config.routing_policy_id == policy.id

    with pytest.raises(CatalogReferenceError):
        await GatewayConfigurationService(gateway_session).upsert(
            "owner-1",
            application.id,
            GatewayConfigUpsert(model_pool_id="missing", routing_policy_id=policy.id),
        )
