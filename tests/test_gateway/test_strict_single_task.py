import json
from types import SimpleNamespace

import pytest

from src.backend_ai.gateway.aggregation.no_aggregator import NoAggregator
from src.shared.dtos.gateway import (
    DecompositionResult,
    Difficulty,
    EffectiveConstraints,
    GatewayRequest,
    RequestConstraints,
    SubTask,
    TaskType,
)


@pytest.mark.asyncio
async def test_no_aggregator_rejects_non_json_passthrough_for_strict_json():
    decomposition = DecompositionResult(
        original_request_id="strict-single-request",
        decomposition_applied=False,
        subtasks=[
            SubTask(
                task_id="task-1",
                description="answer",
                task_type=TaskType.GENERAL,
                difficulty=Difficulty.EASY,
                expected_output="JSON answer",
            )
        ],
        execution_mode="single",
        decomposition_reason="test",
    )
    outcome = await NoAggregator().aggregate(
        GatewayRequest(request_id="strict-single-request", application_id="app", prompt="answer"),
        decomposition,
        [SimpleNamespace(task_id="task-1", status="success", result="offline:not-json")],
        EffectiveConstraints(constraints=RequestConstraints(output_format="json"), resolved={}),
    )

    assert outcome.result.status.value == "failed"
    assert outcome.result.final_answer is None
    assert "valid JSON" in outcome.result.reason
    with pytest.raises(json.JSONDecodeError):
        json.loads(outcome.result.final_answer or "not-json")
