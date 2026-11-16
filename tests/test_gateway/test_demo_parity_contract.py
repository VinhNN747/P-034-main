import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend_ai.agents.routing.deterministic_router import DeterministicRouter
from src.backend_ai.gateway.aggregation.concat_aggregator import ConcatAggregator
from src.backend_ai.gateway.pipeline.orchestrator import GatewayOrchestrator
from src.backend_ai.gateway.providers.base import ProviderClient, ProviderResponse
from src.shared.core.pricing import PricingRegistry
from src.shared.database.base import Base
from src.shared.dtos.gateway import (
    DecompositionResult,
    Difficulty,
    EffectiveConstraints,
    GatewayRequest,
    PipelineResult,
    RequestConstraints,
    SubTask,
    TaskType,
    ValidationResult,
)
from src.shared.services.gateway_persistence_service import GatewayPersistenceService


class StaticPipeline:
    def __init__(self, subtasks):
        self.subtasks = subtasks
        self.pricing = PricingRegistry()

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
                decomposition_applied=True,
                subtasks=self.subtasks,
                execution_mode="hybrid",
                decomposition_reason="contract test",
            ),
        )


class RecordingProvider(ProviderClient):
    name = "recording"

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.events = []
        self.prompts = {}

    async def generate(self, prompt, *, task_id, model_id, timeout_seconds):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.events.append(("start", task_id))
        self.prompts[task_id] = prompt
        await asyncio.sleep(0.01)
        self.events.append(("finish", task_id))
        self.active -= 1
        return ProviderResponse(
            text=f"result:{task_id}",
            provider=self.name,
            model_id=model_id,
            input_tokens=3,
            output_tokens=2,
            metadata={"cost_usd": 0.01, "cost_estimated": False},
        )


@pytest_asyncio.fixture
async def parity_context(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'parity.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _tasks():
    return [
        SubTask(
            task_id="task-a",
            description="collect source facts",
            task_type=TaskType.RESEARCH,
            difficulty=Difficulty.EASY,
            expected_output="facts",
        ),
        SubTask(
            task_id="task-b",
            description="draft a short outline",
            task_type=TaskType.GENERAL,
            difficulty=Difficulty.EASY,
            expected_output="outline",
        ),
        SubTask(
            task_id="task-c",
            description="combine the facts and outline",
            task_type=TaskType.GENERAL,
            difficulty=Difficulty.EASY,
            dependencies=["task-a", "task-b"],
            input_from={"task-a": "facts", "task-b": "outline"},
            expected_output="combined answer",
        ),
    ]


@pytest.mark.asyncio
async def test_full_offline_contract_preserves_waves_handoffs_costs_and_persistence(parity_context):
    provider = RecordingProvider()
    tasks = _tasks()
    request = GatewayRequest(
        request_id="parity-request",
        application_id="parity-app",
        prompt="combine source material",
        metadata={
            "understander": "rule",
            "decomposer": "deterministic",
            "router": "deterministic",
            "aggregator": "deterministic",
        },
    )
    pipeline = StaticPipeline(tasks)
    service = GatewayPersistenceService(parity_context)
    orchestrator = GatewayOrchestrator(
        pipeline,
        routers={
            "deterministic": DeterministicRouter(
                model_by_tier={"small_fast": "fake-model", "mid": "fake-model", "large_reasoning": "fake-model"}
            )
        },
        aggregators={"deterministic": ConcatAggregator()},
        providers={"fake-model": provider},
        persistence=service,
    )

    result = await orchestrator.run(
        request,
        understander="rule",
        decomposer="deterministic",
        router="deterministic",
        aggregator="deterministic",
    )

    assert provider.max_active == 2
    assert provider.events.index(("finish", "task-a")) < provider.events.index(("start", "task-c"))
    assert provider.events.index(("finish", "task-b")) < provider.events.index(("start", "task-c"))
    assert "result:task-a" in provider.prompts["task-c"]
    assert "result:task-b" in provider.prompts["task-c"]
    assert result.aggregation.status.value == "complete"
    assert len(result.execution_results) == 3
    assert result.costs.total_cost == pytest.approx(0.03)
    assert len([trace for trace in result.traces if trace.stage.startswith("route_execute:")]) == 3

    request_record = await service.get_request("parity-request")
    assert request_record is not None
    assert request_record.strategies_json["router"] == "deterministic"
    assert len(await service.list_executions("parity-request")) == 3
    decisions = await service.list_route_decisions("parity-request")
    assert decisions[0].strategy == "deterministic"
    assert len(await service.list_traces("parity-request")) >= 3

    serialized = json.dumps(request_record.response_json)
    assert "OPENAI_API_KEY" not in serialized
    assert "TYPESAFE_API_KEY" not in serialized
    assert "sk-test-secret" not in serialized
    assert "typesafe-test-secret" not in serialized
