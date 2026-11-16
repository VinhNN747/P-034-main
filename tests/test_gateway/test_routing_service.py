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
from src.backend.services.catalog_service import CatalogService
from src.backend.services.gateway_configuration_service import GatewayConfigurationService
from src.backend_ai.services.gateway_routing_service import DatabaseRoutingService, RoutingError
from src.shared.database.base import Base
from src.shared.dtos.gateway import Difficulty, QualityPriority, RequestConstraints, SubTask, TaskType
from src.shared.models.gateway import ApplicationGatewayConfig
from src.shared.services.gateway_runtime_service import GatewayRuntimeService


@pytest_asyncio.fixture
async def routing_context(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'routing.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        application = await ApplicationService(session).create(
            "owner-1", ApplicationCreate(slug="routing-demo", name="Routing demo")
        )
        catalog = CatalogService(session)
        local = await catalog.create_provider(ProviderCreate(name="local", display_name="Local", kind="offline"))
        external = await catalog.create_provider(
            ProviderCreate(name="external", display_name="External", kind="openai")
        )
        cheap = await catalog.create_model(
            ModelCreate(
                provider_id=local.id,
                name="cheap-fast",
                display_name="Cheap fast",
                capabilities=["chat", "classification", "text"],
                target_latency_ms=100,
                input_price_per_million=0.1,
                output_price_per_million=0.2,
            )
        )
        quality = await catalog.create_model(
            ModelCreate(
                provider_id=local.id,
                name="quality-reasoner",
                display_name="Quality reasoner",
                capabilities=["chat", "reasoning"],
                target_latency_ms=800,
                input_price_per_million=5.0,
                output_price_per_million=10.0,
            )
        )
        blocked = await catalog.create_model(
            ModelCreate(
                provider_id=external.id,
                name="blocked-external",
                display_name="Blocked external",
                capabilities=["chat", "reasoning"],
                target_latency_ms=50,
            )
        )
        pool = await catalog.create_pool(
            ModelPoolCreate(
                name="general",
                purpose="gateway",
                members=[
                    ModelPoolMemberCreate(model_id=quality.id),
                    ModelPoolMemberCreate(model_id=cheap.id),
                    ModelPoolMemberCreate(model_id=blocked.id),
                ],
            ),
            "owner-1",
        )
        policy = await catalog.create_policy(
            PolicyCreate(
                name="default-routing",
                guardrails={"quality_scores": {quality.id: 100, cheap.id: 30, blocked.id: 90}},
                fallback_chain=[cheap.id, quality.id],
            )
        )
        await GatewayConfigurationService(session).upsert(
            "owner-1",
            application.id,
            GatewayConfigUpsert(
                model_pool_id=pool.id,
                routing_policy_id=policy.id,
                fallback_model_id=cheap.id,
            ),
        )
        yield {
            "session": session,
            "application_id": application.id,
            "cheap": cheap.id,
            "quality": quality.id,
            "blocked": blocked.id,
        }
    await engine.dispose()


def _task(*, capabilities: list[str] | None = None) -> SubTask:
    return SubTask(
        task_id="task-1",
        description="classify the request",
        task_type=TaskType.CLASSIFICATION,
        difficulty=Difficulty.MEDIUM,
        required_capabilities=capabilities or ["chat"],
        expected_output="label",
    )


def test_application_latency_objective_is_applied_to_default_request_constraints():
    config = ApplicationGatewayConfig(
        application_id="application",
        model_pool_id="pool",
        routing_policy_id="policy",
        routing_objective="latency_first",
        max_output_tokens=800,
        max_latency_ms=700,
    )

    resolved = GatewayRuntimeService.apply_config_limits(
        RequestConstraints(max_latency_ms=1_000),
        config,
    )

    assert resolved.quality_priority == QualityPriority.LATENCY_FIRST
    assert resolved.max_latency_ms == 700


@pytest.mark.asyncio
async def test_runtime_catalog_loads_pricing_and_provider_aliases_from_database(routing_context):
    pricing, provider_rows = await GatewayRuntimeService(routing_context["session"]).model_runtime_catalog()

    cheap = pricing.get("cheap-fast")
    assert cheap is not None
    assert cheap.input_price_per_million == 0.1
    assert any(model.name == "cheap-fast" and provider.kind == "offline" for model, provider in provider_rows)


@pytest.mark.asyncio
async def test_routing_filters_blocked_provider_and_missing_capabilities(routing_context):
    service = DatabaseRoutingService(routing_context["session"], routing_context["application_id"])

    decision = await service.route(
        _task(),
        RequestConstraints(quality_priority="quality_first", blocked_providers={"external"}),
    )

    assert decision.selected_model == routing_context["quality"]
    assert decision.fallback_models == [routing_context["cheap"]]
    assert decision.input_price_per_million == 5.0
    assert decision.output_price_per_million == 10.0
    assert decision.pricing_estimated is True
    assert decision.provider_name == "local"
    assert decision.provider_kind == "offline"
    assert decision.provider_model == "quality-reasoner"
    assert decision.fallback_provider_kinds[routing_context["cheap"]] == "offline"
    assert decision.fallback_provider_models[routing_context["cheap"]] == "cheap-fast"

    with pytest.raises(RoutingError, match="capabilities"):
        await service.route(_task(capabilities=["vision"]))


@pytest.mark.asyncio
async def test_balanced_score_tie_uses_model_id_without_pool_order(routing_context):
    service = DatabaseRoutingService(routing_context["session"], routing_context["application_id"])
    decision = await service.route(_task(), RequestConstraints(blocked_providers={"external"}))
    expected = sorted([routing_context["quality"], routing_context["cheap"]])
    assert decision.selected_model == expected[0]
    assert decision.fallback_models == expected[1:]


@pytest.mark.asyncio
async def test_routing_priorities_and_fallback_order_are_deterministic(routing_context):
    service = DatabaseRoutingService(routing_context["session"], routing_context["application_id"])

    quality = await service.route(_task(), RequestConstraints(quality_priority="quality_first"))
    cost = await service.route(
        _task(), RequestConstraints(quality_priority="cost_first", blocked_providers={"external"})
    )
    latency_first = await service.route(
        _task(), RequestConstraints(quality_priority="latency_first", blocked_providers={"external"})
    )
    latency = await service.route(_task(), RequestConstraints(max_latency_ms=200, blocked_providers={"external"}))

    assert quality.selected_model == routing_context["quality"]
    assert cost.selected_model == routing_context["cheap"]
    assert latency_first.selected_model == routing_context["cheap"]
    assert latency.selected_model == routing_context["cheap"]
    assert quality.fallback_models == [routing_context["blocked"], routing_context["cheap"]]


@pytest.mark.asyncio
async def test_objective_can_trade_native_capability_for_cost_when_alias_is_supported(
    routing_context,
):
    service = DatabaseRoutingService(routing_context["session"], routing_context["application_id"])
    task = _task(capabilities=["reasoning"])
    common = {"blocked_providers": {"external"}}

    cost = await service.route(
        task,
        RequestConstraints(quality_priority="cost_first", **common),
    )
    quality = await service.route(
        task,
        RequestConstraints(quality_priority="quality_first", **common),
    )

    assert cost.selected_model == routing_context["cheap"]
    assert quality.selected_model == routing_context["quality"]
