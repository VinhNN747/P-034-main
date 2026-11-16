from dataclasses import dataclass, field

import pytest

from src.backend_ai.agents.routing.deterministic_router import DeterministicRouter
from src.backend_ai.gateway.aggregation.base import AggregationOutcome, BaseAggregator
from src.backend_ai.gateway.aggregation.concat_aggregator import ConcatAggregator
from src.backend_ai.gateway.aggregation.no_aggregator import NoAggregator
from src.backend_ai.gateway.pipeline.orchestrator import GatewayOrchestrator
from src.backend_ai.gateway.providers.offline import OfflineProvider
from src.shared.core.pricing import PricingRegistry
from src.shared.dtos.gateway import (
    AggregationResult,
    AggregationStatus,
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


@dataclass
class StaticPipeline:
    subtasks: list[SubTask]
    pricing: PricingRegistry = field(default_factory=PricingRegistry)

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
                decomposition_applied=len(self.subtasks) > 1,
                subtasks=self.subtasks,
                execution_mode="parallel" if len(self.subtasks) > 1 else "single",
                decomposition_reason="test pipeline",
            ),
        )


class RecordingAggregator(BaseAggregator):
    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    async def aggregate(self, request, decomposition, execution_results, constraints, goal_extraction=None):
        self.calls += 1
        return AggregationOutcome(
            result=AggregationResult(
                original_request_id=request.request_id,
                status=AggregationStatus.COMPLETE,
                final_answer=f"aggregated-by:{self.name}",
                strategy=self.name,
                reason="test aggregation",
            ),
            strategy=self.name,
        )


def _task(task_id: str) -> SubTask:
    return SubTask(
        task_id=task_id,
        description=f"complete {task_id}",
        task_type=TaskType.GENERAL,
        difficulty=Difficulty.EASY,
        expected_output="result",
    )


def _orchestrator(subtasks, aggregators):
    return GatewayOrchestrator(
        StaticPipeline(subtasks),
        routers={"deterministic": DeterministicRouter()},
        aggregators=aggregators,
        providers={"offline": OfflineProvider()},
    )


@pytest.mark.asyncio
async def test_auto_uses_llm_for_two_subtasks_and_records_selection_metadata():
    llm = RecordingAggregator("llm")
    request = GatewayRequest(request_id="auto-llm", application_id="app", prompt="two tasks")
    orchestrator = _orchestrator(
        [_task("task-1"), _task("task-2")],
        {"none": NoAggregator(), "deterministic": ConcatAggregator(), "llm": llm},
    )

    result = await orchestrator.run(request, router="deterministic", aggregator="auto")

    assert llm.calls == 1
    assert result.aggregation is not None
    assert result.aggregation.strategy == "llm"
    assert result.request.metadata["aggregator_requested"] == "auto"
    assert result.request.metadata["aggregator_effective"] == "llm"
    assert result.request.metadata["aggregator_selection_reason"] == "multiple_tasks_llm_available"
    aggregate_trace = next(trace for trace in result.traces if trace.stage == "aggregate")
    assert aggregate_trace.metadata["aggregator_requested"] == "auto"
    assert aggregate_trace.metadata["aggregator_effective"] == "llm"


@pytest.mark.asyncio
async def test_auto_falls_back_to_deterministic_for_two_subtasks_without_llm():
    deterministic = RecordingAggregator("deterministic")
    result = await _orchestrator(
        [_task("task-1"), _task("task-2")],
        {"none": NoAggregator(), "deterministic": deterministic},
    ).run(
        GatewayRequest(request_id="auto-deterministic", application_id="app", prompt="two tasks"),
        router="deterministic",
        aggregator="auto",
    )

    assert deterministic.calls == 1
    assert result.aggregation is not None
    assert result.aggregation.strategy == "deterministic"
    assert result.request.metadata["aggregator_effective"] == "deterministic"


@pytest.mark.asyncio
async def test_auto_uses_no_aggregator_for_one_subtask():
    result = await _orchestrator(
        [_task("task-1")],
        {"none": NoAggregator(), "deterministic": RecordingAggregator("deterministic")},
    ).run(
        GatewayRequest(request_id="auto-none", application_id="app", prompt="one task"),
        router="deterministic",
        aggregator="auto",
    )

    assert result.aggregation is not None
    assert result.aggregation.strategy == "none"
    assert result.request.metadata["aggregator_effective"] == "none"
