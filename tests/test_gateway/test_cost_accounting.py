from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.dtos.applications import ApplicationCreate
from src.backend.services.application_service import ApplicationService
from src.backend_ai.agents.routing.base import BaseRouter, RouteDecision
from src.backend_ai.agents.routing.executor import SubtaskExecutor
from src.backend_ai.agents.routing.router import ExecutionConfig
from src.backend_ai.gateway.aggregation.base import AggregationOutcome, BaseAggregator
from src.backend_ai.gateway.pipeline.orchestrator import GatewayOrchestrator
from src.backend_ai.gateway.providers.base import ProviderClient, ProviderResponse, ProviderStreamEvent
from src.shared.core.pricing import ModelPricing, PricingRegistry
from src.shared.database.base import Base
from src.shared.dtos.gateway import (
    AggregationResult,
    AggregationStatus,
    DecompositionResult,
    Difficulty,
    EffectiveConstraints,
    GatewayRequest,
    ModelUsage,
    OutputEvidence,
    PipelineResult,
    RequestConstraints,
    SubTask,
    TaskType,
    ValidationResult,
)
from src.shared.models.billing import BillingTransactionRecord
from src.shared.services.billing_service import BillingService


class PricedRouter(BaseRouter):
    name = "database"

    async def aroute(self, subtask, upstream_results=None, **kwargs):
        return RouteDecision(
            task_id=subtask.task_id,
            tier="catalog-model-id",
            strategy=self.name,
            selected_model="catalog-model-id",
            provider_name="fake-openai",
            provider_kind="openai",
            provider_model="catalog-model",
            input_price_per_million=0.15,
            output_price_per_million=0.60,
            pricing_version="test-pricing",
            pricing_estimated=True,
        )


class TokenProvider(ProviderClient):
    name = "fake-openai"

    async def generate(self, prompt, *, task_id, model_id, timeout_seconds):
        return ProviderResponse(
            text="provider result",
            provider=self.name,
            model_id=model_id,
            input_tokens=1000,
            output_tokens=500,
        )


def _task() -> SubTask:
    return SubTask(
        task_id="task-1",
        description="complete the task",
        task_type=TaskType.GENERAL,
        difficulty=Difficulty.EASY,
        expected_output="answer",
    )


@pytest.mark.asyncio
async def test_db_routed_execution_uses_catalog_prices_when_provider_has_no_cost_metadata():
    result = await SubtaskExecutor(
        PricedRouter(),
        providers={"fake-openai": TokenProvider()},
        timeout_seconds=2,
    ).aexecute_subtasks([_task()])

    assert result[0].status == "success"
    assert result[0].provider == "fake-openai"
    assert result[0].cost_usd == pytest.approx(0.00045)
    assert result[0].cost_estimated is True


class AnthropicUsageProvider(ProviderClient):
    name = "anthropic"

    def _response(self):
        return ProviderResponse(
            text="provider result",
            provider=self.name,
            model_id="claude-haiku-5-5",
            input_tokens=100_001,
            output_tokens=1_000,
            usage_received=True,
            output_evidence=OutputEvidence(cached_tokens=100_000),
        )

    async def generate(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
        return self._response()

    async def stream(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
        yield ProviderStreamEvent(type="text_delta", text="provider result")
        yield ProviderStreamEvent(type="final", final=self._response())


def _haiku_pricing_registry():
    return PricingRegistry(
        [
            ModelPricing(
                model_id="catalog-model-id",
                provider="anthropic",
                input_price_per_million=0.10,
                output_price_per_million=0.50,
                pricing_version="anthropic-docs-2026-10-17",
                pricing_rules={
                    "standard_input_per_million": 0.10,
                    "standard_output_per_million": 0.50,
                    "long_context_input_threshold": 100_000,
                    "long_context_threshold_operator": ">",
                    "long_context_input_per_million": 0.50,
                    "long_context_output_per_million": 2.50,
                    "cache_write_5m_input_per_million": 0.125,
                    "cache_write_1h_input_per_million": 0.20,
                    "cache_read_input_per_million": 0.01,
                },
            )
        ]
    )


@pytest.mark.asyncio
async def test_anthropic_stream_and_nonstream_execution_share_database_usage_pricing():
    from src.backend_ai.services import runtime_stream_service
    from src.backend_ai.services.runtime_stream_service import RuntimeStreamSession

    providers = {"fake-openai": AnthropicUsageProvider()}
    registry = _haiku_pricing_registry()
    nonstream = await SubtaskExecutor(
        PricedRouter(), providers=providers, pricing=registry
    ).aexecute_subtask(_task())

    stream_session = RuntimeStreamSession()
    token = runtime_stream_service._current.set(stream_session)
    try:
        streamed = await SubtaskExecutor(
            PricedRouter(), providers=providers, pricing=registry,
            execution_config=ExecutionConfig(stream_output=True, stream_task_ids=("task-1",)),
        ).aexecute_subtask(_task())
    finally:
        runtime_stream_service._current.reset(token)

    assert nonstream.cost_usd == pytest.approx(0.0075005)
    assert streamed.cost_usd == pytest.approx(nonstream.cost_usd)
    assert nonstream.output_evidence.cached_tokens == 100_000


@pytest.mark.asyncio
async def test_runtime_catalog_carries_database_pricing_metadata_into_registry():
    from src.shared.models.gateway import Model, Provider
    from src.shared.services.gateway_runtime_service import GatewayRuntimeService

    class Rows:
        def all(self):
            return [(model, provider)]

    class Session:
        async def execute(self, _statement):
            return Rows()

    provider = Provider(id="anthropic-provider", name="anthropic", display_name="Anthropic", kind="anthropic",
        purpose="target", status="active")
    model = Model(id="haiku-id", provider_id=provider.id, name="claude-haiku-5-5",
        display_name="Claude Haiku 5.5", status="active", input_price_per_million=0.10,
        output_price_per_million=0.50, pricing_version="anthropic-docs-2026-10-17",
        feature_metadata_json={"pricing": {
            "standard_input_per_million": 0.10,
            "standard_output_per_million": 0.50,
            "long_context_input_threshold": 100_000,
            "long_context_threshold_operator": ">",
            "long_context_input_per_million": 0.50,
            "long_context_output_per_million": 2.50,
            "cache_write_5m_input_per_million": 0.125,
            "cache_write_1h_input_per_million": 0.20,
            "cache_read_input_per_million": 0.01,
        }})

    pricing, rows = await GatewayRuntimeService(Session()).model_runtime_catalog()

    assert rows == [(model, provider)]
    assert pricing.calculate("haiku-id", 100_001, 1_000)[0] == pytest.approx(0.0525005)


@dataclass
class PricedPipeline:
    pricing: PricingRegistry = field(
        default_factory=lambda: PricingRegistry(
            [
                ModelPricing(
                    model_id="agg-model",
                    provider="openai",
                    input_price_per_million=2.0,
                    output_price_per_million=10.0,
                    pricing_version="test-pricing",
                )
            ]
        )
    )

    async def run(self, request, **kwargs):
        return PipelineResult(
            request=request,
            validation=ValidationResult(accepted=True),
            effective_constraints=EffectiveConstraints(
                constraints=RequestConstraints(),
                resolved={},
            ),
            decomposition=DecompositionResult(
                original_request_id=request.request_id,
                decomposition_applied=False,
                subtasks=[_task()],
                execution_mode="single",
                decomposition_reason="cost test",
            ),
        )


class UsageAggregator(BaseAggregator):
    name = "none"

    async def aggregate(self, request, decomposition, execution_results, constraints, goal_extraction=None):
        return AggregationOutcome(
            result=AggregationResult(
                original_request_id=request.request_id,
                status=AggregationStatus.COMPLETE,
                final_answer="aggregated",
                strategy=self.name,
                reason="cost test",
            ),
            strategy=self.name,
            model="agg-model",
            usage=ModelUsage(input_tokens=1000, output_tokens=500),
        )


@pytest.mark.asyncio
async def test_aggregator_model_usage_is_included_in_total_cost():
    result = await GatewayOrchestrator(
        PricedPipeline(),
        routers={"database": PricedRouter()},
        aggregators={"none": UsageAggregator()},
        providers={"fake-openai": TokenProvider()},
    ).run(
        GatewayRequest(request_id="aggregation-cost", application_id="app", prompt="answer"),
        router="database",
        aggregator="none",
    )

    assert result.costs.aggregation_cost == pytest.approx(0.007)
    assert result.costs.total_cost == pytest.approx(0.00745)


@pytest_asyncio.fixture
async def billing_runtime_context(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'billing-runtime.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        application = await ApplicationService(session).create(
            "billing-owner", ApplicationCreate(slug="billing-runtime", name="Billing runtime")
        )
        account = await BillingService(session).account("billing-owner")
        account.balance = 100
        await session.commit()
        yield session, application.id, account.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_aggregation_does_not_create_usage_debit(billing_runtime_context):
    session, application_id, account_id = billing_runtime_context

    transaction = await BillingService(session).debit_runtime_request(
        application_id,
        "failed-aggregation-request",
        0.00745,
        "failed",
    )

    balance = await session.scalar(
        select(func.sum(BillingTransactionRecord.amount)).where(BillingTransactionRecord.account_id == account_id)
    )
    assert transaction is None
    assert balance in (None, 0)
