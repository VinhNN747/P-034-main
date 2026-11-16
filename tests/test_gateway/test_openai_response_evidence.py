from types import SimpleNamespace

import pytest

from src.backend_ai.dtos.llm import LLMAggregationResult
from src.backend_ai.gateway.aggregation.concat_aggregator import ConcatAggregator
from src.backend_ai.gateway.aggregation.llm_aggregator import LLMAggregator
from src.backend_ai.gateway.aggregation.no_aggregator import NoAggregator
from src.shared.dtos.gateway import (
    DecompositionResult,
    Difficulty,
    EffectiveConstraints,
    GatewayRequest,
    ModelUsage,
    OutputEvidence,
    RequestConstraints,
    StructuredModelResponse,
    SubTask,
    TaskType,
)


def _decomposition() -> DecompositionResult:
    return DecompositionResult(
        original_request_id="request-1",
        decomposition_applied=True,
        subtasks=[
            SubTask(task_id="task-1", description="answer", task_type=TaskType.GENERAL,
                    difficulty=Difficulty.EASY, expected_output="text"),
            SubTask(task_id="task-2", description="format", task_type=TaskType.GENERAL,
                    difficulty=Difficulty.EASY, expected_output="text"),
        ],
        execution_mode="parallel",
        decomposition_reason="test",
    )


def _constraints():
    return EffectiveConstraints(constraints=RequestConstraints(), resolved={})


@pytest.mark.asyncio
async def test_concat_propagates_most_conservative_finish_reason():
    outcome = await ConcatAggregator().aggregate(
        GatewayRequest(request_id="request-1", application_id="app", prompt="two steps"),
        _decomposition(),
        [
            SimpleNamespace(task_id="task-1", status="success", result="one",
                            output_evidence=OutputEvidence(finish_reason="stop")),
            SimpleNamespace(task_id="task-2", status="success", result="two",
                            output_evidence=OutputEvidence(finish_reason="length")),
        ],
        _constraints(),
    )
    assert outcome.output_evidence.finish_reason == "length"
    assert outcome.result.output_evidence.finish_reason == "length"


@pytest.mark.asyncio
async def test_no_aggregator_preserves_final_subtask_evidence():
    decomposition = _decomposition().model_copy(update={"subtasks": _decomposition().subtasks[:1]})
    outcome = await NoAggregator().aggregate(
        GatewayRequest(request_id="request-1", application_id="app", prompt="answer"),
        decomposition,
        [SimpleNamespace(task_id="task-1", status="success", result="answer",
                          output_evidence=OutputEvidence(finish_reason="content_filter"))],
        _constraints(),
    )
    assert outcome.output_evidence.finish_reason == "content_filter"


@pytest.mark.asyncio
async def test_llm_aggregator_finish_evidence_describes_final_producer():
    class Client:
        async def generate(self, prompt, schema, model, timeout):
            assert schema is LLMAggregationResult
            return StructuredModelResponse(
                data={"final_answer": "synthesized"},
                usage=ModelUsage(input_tokens=8, output_tokens=2),
                model=model,
                output_evidence=OutputEvidence(finish_reason="length"),
            )

    decomposition = _decomposition()
    decomposition = decomposition.model_copy(update={"subtasks": decomposition.subtasks[:1]})
    outcome = await LLMAggregator(Client()).aggregate(
        GatewayRequest(request_id="request-1", application_id="app", prompt="answer"),
        decomposition,
        [SimpleNamespace(task_id="task-1", status="success", result="source",
                          output_evidence=OutputEvidence(finish_reason="stop"))],
        _constraints(),
    )
    assert outcome.result.final_answer == "synthesized"
    assert outcome.output_evidence.finish_reason == "length"
