from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.dtos.catalog import (
    ModelCreate,
    ModelPoolCreate,
    ModelPoolMemberCreate,
    PolicyCreate,
    ProviderCreate,
)
from src.backend.services.catalog_service import (
    CatalogConflictError,
    CatalogReferenceError,
    CatalogService,
)
from src.shared.database.base import Base


@pytest_asyncio.fixture
async def catalog_session(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_provider_names_are_unique_and_raw_secrets_are_rejected(catalog_session):
    service = CatalogService(catalog_session)
    provider = await service.create_provider(
        ProviderCreate(
            name="openai",
            display_name="OpenAI",
            kind="openai",
        )
    )

    assert not hasattr(provider, "secret_env_var")
    assert "api_key" not in provider.model_dump()

    with pytest.raises(CatalogConflictError):
        await service.create_provider(
            ProviderCreate(
                name="openai",
                display_name="Duplicate",
                kind="openai",
            )
        )

    with pytest.raises(ValidationError):
        ProviderCreate(
            name="unsafe",
            display_name="Unsafe",
            kind="openai",
            api_key="raw-secret",
        )


@pytest.mark.asyncio
async def test_pool_is_unordered_and_rejects_inactive_models(catalog_session):
    service = CatalogService(catalog_session)
    provider = await service.create_provider(
        ProviderCreate(
            name="local",
            display_name="Local",
            kind="local",
        )
    )
    active = await service.create_model(
        ModelCreate(
            provider_id=provider.id,
            name="fast",
            display_name="Fast",
            capabilities=["chat"],
        )
    )
    inactive = await service.create_model(
        ModelCreate(
            provider_id=provider.id,
            name="inactive",
            display_name="Inactive",
            status="inactive",
            capabilities=["chat"],
        )
    )
    pool = await service.create_pool(
        ModelPoolCreate(
            name="balanced",
            purpose="general",
            members=[
                ModelPoolMemberCreate(model_id=active.id),
            ],
        ),
        "owner-1",
    )

    assert pool.members[0].model_id == active.id
    assert "priority" not in pool.members[0].model_dump()

    with pytest.raises(CatalogReferenceError):
        await service.create_pool(
            ModelPoolCreate(
                name="invalid",
                purpose="general",
                members=[ModelPoolMemberCreate(model_id=inactive.id)],
            ),
            "owner-1",
        )


@pytest.mark.asyncio
async def test_policy_scoring_is_independent_of_model_pools(catalog_session):
    policy = await CatalogService(catalog_session).create_policy(
        PolicyCreate(name="balanced-policy", match_conditions={"environment": "development"}),
    )
    assert policy.name == "balanced-policy"
    assert "pool_id" not in policy.model_dump()
