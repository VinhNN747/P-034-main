import json
from types import SimpleNamespace

import pytest

from src.backend_ai.gateway.aggregation.concat_aggregator import ConcatAggregator
from src.backend_ai.gateway.aggregation.selector import resolve_aggregator
from src.shared.dtos.gateway import (
    DecompositionResult,
    Difficulty,
    EffectiveConstraints,
    GatewayRequest,
    RequestConstraints,
    SubTask,
    TaskType,
)


def test_auto_strict_json_rejects_non_json_capable_deterministic_fallback():
    with pytest.raises(ValueError, match="Strict JSON output requires"):
        resolve_aggregator(
            "auto",
            2,
            {"none": object(), "deterministic": object()},
            strict_json=True,
        )


def test_auto_strict_json_accepts_json_capable_deterministic_fallback():
    selection = resolve_aggregator(
        "auto",
        2,
        {"none": object(), "deterministic": ConcatAggregator()},
        strict_json=True,
    )

    assert selection.selected == "deterministic"
    assert selection.reason == "multiple_tasks_llm_unavailable_json_safe"


def test_explicit_non_json_capable_deterministic_aggregator_is_rejected_for_strict_json():
    with pytest.raises(ValueError, match="Strict JSON output requires"):
        resolve_aggregator(
            "deterministic",
            2,
            {"deterministic": object()},
            strict_json=True,
        )


@pytest.mark.asyncio
async def test_deterministic_aggregator_emits_parseable_json_envelope():
    aggregator = ConcatAggregator()
    decomposition = DecompositionResult(
        original_request_id="strict-json-request",
        decomposition_applied=True,
        subtasks=[
            SubTask(
                task_id="task-1",
                description="calculate",
                task_type=TaskType.REASONING,
                difficulty=Difficulty.EASY,
                expected_output="result",
            ),
            SubTask(
                task_id="task-2",
                description="translate",
                task_type=TaskType.TRANSLATION,
                difficulty=Difficulty.EASY,
                expected_output="result",
            ),
        ],
        execution_mode="parallel",
        decomposition_reason="test",
    )
    outcome = await aggregator.aggregate(
        GatewayRequest(request_id="strict-json-request", application_id="app", prompt="two tasks"),
        decomposition,
        [
            SimpleNamespace(task_id="task-1", status="success", result="378"),
            SimpleNamespace(task_id="task-2", status="success", result="AI Gateway is operating normally"),
        ],
        EffectiveConstraints(constraints=RequestConstraints(output_format="json"), resolved={}),
    )

    payload = json.loads(outcome.result.final_answer or "")
    assert payload["results"] == [
        {"task_id": "task-1", "result": "378"},
        {"task_id": "task-2", "result": "AI Gateway is operating normally"},
    ]
