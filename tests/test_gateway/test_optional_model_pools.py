from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.backend.dtos.applications import ApplicationCreate
from src.backend.dtos.catalog import (
    ModelCreate,
    ModelPoolCreate,
    ModelPoolMemberCreate,
    ModelPoolUpdate,
    ProviderCreate,
)
from src.backend.dtos.gateway_configuration import GatewayConfigUpsert
from src.backend.services.application_service import ApplicationService
from src.backend.services.catalog_service import CatalogReferenceError, CatalogService
from src.backend.services.gateway_configuration_service import GatewayConfigurationService
from src.backend_ai.services.gateway_routing_service import DatabaseRoutingService, RoutingError
from src.shared.database.base import Base
from src.shared.dtos.gateway import Difficulty, RequestConstraints, SubTask, TaskType


@pytest_asyncio.fixture
async def context(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'optional.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        catalog = CatalogService(session)
        provider = await catalog.create_provider(ProviderCreate(name='offline', display_name='Offline', kind='offline'))
        a = await catalog.create_model(ModelCreate(provider_id=provider.id, name='a', display_name='A', capabilities=['chat'], target_latency_ms=100, input_price_per_million=1))
        b = await catalog.create_model(ModelCreate(provider_id=provider.id, name='b', display_name='B', capabilities=['chat'], target_latency_ms=100, input_price_per_million=2))
        app = await ApplicationService(session).create('alice', ApplicationCreate(name='Demo', slug='demo'))
        yield session, catalog, provider, a, b, app
    await engine.dispose()


def task():
    return SubTask(task_id='t', description='chat', task_type=TaskType.CLASSIFICATION, difficulty=Difficulty.MEDIUM, required_capabilities=['chat'], expected_output='text')


@pytest.mark.asyncio
async def test_default_application_routes_full_dynamic_catalog(context):
    session, catalog, provider, a, b, app = context
    config = await GatewayConfigurationService(session).get('alice', app.id)
    assert config.model_pool_id is None
    assert config.routing_policy_id is None
    assert set(config.model_ids) == {a.id, b.id}
    c = await catalog.create_model(ModelCreate(provider_id=provider.id, name='c', display_name='C', capabilities=['chat'], target_latency_ms=100, input_price_per_million=0.1))
    route = await DatabaseRoutingService(session, app.id).route(task(), RequestConstraints(quality_priority='cost_first'))
    assert route.selected_model == c.id
    assert set(route.fallback_models) == {a.id, b.id}


@pytest.mark.asyncio
async def test_pool_is_private_subset_and_can_be_cleared(context):
    session, catalog, _, a, b, app = context
    pool = await catalog.create_pool(ModelPoolCreate(name='subset', purpose='chat', members=[ModelPoolMemberCreate(model_id=b.id)]), 'alice')
    assert pool.owner_user_id == 'alice'
    assert 'priority' not in pool.members[0].model_dump()
    assert 'enabled' not in pool.members[0].model_dump()
    assert 'routing_strategy' not in pool.model_dump()
    config_service = GatewayConfigurationService(session)
    await config_service.upsert('alice', app.id, GatewayConfigUpsert(model_pool_id=pool.id))
    route = await DatabaseRoutingService(session, app.id).route(task())
    assert route.selected_model == b.id
    assert route.fallback_models == []
    # Omitted selection preserves the current pool; explicit null clears it.
    preserved = await config_service.upsert('alice', app.id, GatewayConfigUpsert(budget_ceiling=5))
    assert preserved.model_pool_id == pool.id
    cleared = await config_service.upsert('alice', app.id, GatewayConfigUpsert(model_pool_id=None))
    assert cleared.model_pool_id is None
    assert set(cleared.model_ids) == {a.id, b.id}
    assert await catalog.list_pools('bob') == []
    with pytest.raises(CatalogReferenceError):
        await catalog.get_pool(pool.id, 'bob')
    with pytest.raises(CatalogReferenceError):
        await catalog.update_pool(pool.id, ModelPoolUpdate(name='stolen', purpose='chat', members=[ModelPoolMemberCreate(model_id=a.id)]), 'bob')
    bob_app = await ApplicationService(session).create('bob', ApplicationCreate(name='Bob', slug='bob'))
    with pytest.raises(CatalogReferenceError):
        await config_service.upsert('bob', bob_app.id, GatewayConfigUpsert(model_pool_id=pool.id))


def test_pool_contract_rejects_priority_and_empty_set():
    with pytest.raises(ValidationError):
        ModelPoolMemberCreate(model_id='a', priority=1)
    with pytest.raises(ValidationError):
        ModelPoolCreate(name='empty', purpose='chat', members=[])


@pytest.mark.asyncio
async def test_selected_inactive_pool_does_not_expand_to_catalog(context):
    session, catalog, _, a, _, app = context
    pool = await catalog.create_pool(ModelPoolCreate(name='subset', purpose='chat', members=[ModelPoolMemberCreate(model_id=a.id)]), 'alice')
    await GatewayConfigurationService(session).upsert('alice', app.id, GatewayConfigUpsert(model_pool_id=pool.id))
    await catalog.update_pool(pool.id, ModelPoolUpdate(name='subset', purpose='chat', status='inactive', members=[ModelPoolMemberCreate(model_id=a.id)]), 'alice')
    with pytest.raises(RoutingError, match='pool'):
        await DatabaseRoutingService(session, app.id).route(task())


@pytest.mark.asyncio
async def test_membership_insertion_order_does_not_change_ranking(context):
    session, catalog, _, a, b, app = context
    pool = await catalog.create_pool(ModelPoolCreate(name='order-free',purpose='chat',members=[ModelPoolMemberCreate(model_id=a.id),ModelPoolMemberCreate(model_id=b.id)]),'alice')
    await GatewayConfigurationService(session).upsert('alice',app.id,GatewayConfigUpsert(model_pool_id=pool.id))
    router = DatabaseRoutingService(session,app.id)
    objectives=['balanced','cost_first','latency_first','quality_first']
    before=[await router.route(task(),RequestConstraints(quality_priority=o)) for o in objectives]
    await catalog.update_pool(pool.id,ModelPoolUpdate(name='order-free',purpose='chat',members=[ModelPoolMemberCreate(model_id=b.id),ModelPoolMemberCreate(model_id=a.id)]),'alice')
    after=[await router.route(task(),RequestConstraints(quality_priority=o)) for o in objectives]
    assert [(r.selected_model,r.fallback_models) for r in before] == [(r.selected_model,r.fallback_models) for r in after]
