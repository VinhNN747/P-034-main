import pytest

from src.backend_ai.gateway.decomposition.validator import DecompositionValidator
from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.gateway.validation.constraint_resolver import ConstraintResolver
from src.shared.core.pricing import ModelPricing, PricingRegistry
from src.shared.dtos.gateway import (
    DecompositionResult,
    Difficulty,
    GatewayRequest,
    RequestConstraints,
    SubTask,
    TaskType,
)


@pytest.mark.asyncio
async def test_ported_pipeline_emits_all_pre_routing_stages():
    request = GatewayRequest(
        request_id="pipeline-port-1",
        application_id="tests",
        prompt="Summarize this report in Vietnamese and output JSON.",
    )

    result = await RequestPipeline().run(request, understander="rule", decomposer="deterministic")

    assert result.validation.accepted
    assert result.decomposition is not None
    assert result.execution_results is None
    assert result.aggregation is None
    assert [trace.stage for trace in result.traces] == [
        "request_validation",
        "goal_extraction",
        "constraint_resolution",
        "understand",
        "request_scoring",
        "decomposition_decision",
        "decompose",
        "decomposition_validation",
    ]
    assert result.effective_constraints.constraints.output_format == "json"


@pytest.mark.asyncio
async def test_rejected_request_skips_remaining_pre_routing_stages():
    request = GatewayRequest(
        request_id="pipeline-port-2",
        application_id="tests",
        prompt="   ",
    )

    result = await RequestPipeline().run(request)

    assert not result.validation.accepted
    assert len(result.traces) == 8
    assert all(trace.status == "skipped" for trace in result.traces[1:])


def test_ported_graph_validator_rejects_cycles_and_missing_hard_constraint():
    request = GatewayRequest(
        request_id="pipeline-port-3",
        application_id="tests",
        prompt="Summarize the report",
        constraints=RequestConstraints(output_format="json"),
    )
    constraints = ConstraintResolver().resolve(request)

    def task(task_id: str, dependencies: list[str], constraints: dict) -> SubTask:
        return SubTask(
            task_id=task_id,
            description="Summarize the report",
            task_type=TaskType.SUMMARIZATION,
            difficulty=Difficulty.EASY,
            dependencies=dependencies,
            constraints=constraints,
            expected_output="A summary",
        )

    result = DecompositionResult(
        original_request_id=request.request_id,
        decomposition_applied=True,
        subtasks=[
            task("one", ["two"], {"output_format": "json"}),
            task("two", ["one"], {}),
        ],
        execution_mode="hybrid",
        preserved_constraints={},
        decomposition_reason="test",
    )

    report = DecompositionValidator().validate(request, result, constraints)

    assert not report.valid
    assert "DEPENDENCY_CYCLE" in report.errors
    assert any(item.startswith("HARD_CONSTRAINT_NOT_PRESERVED") for item in report.errors)


def test_ported_pricing_registry_calculates_known_costs():
    registry = PricingRegistry(
        [
            ModelPricing(
                model_id="test-model",
                provider="local",
                input_price_per_million=2.0,
                output_price_per_million=4.0,
                pricing_version="test",
                estimated=True,
            )
        ]
    )

    cost, estimated = registry.calculate("test-model", 1_000_000, 500_000)

    assert cost == 4.0
    assert estimated is True
