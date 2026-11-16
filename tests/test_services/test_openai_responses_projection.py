from datetime import UTC, datetime

import pytest
from openai.types.responses import Response as SDKResponse

from src.backend_ai.dtos.openai_responses import ResponseCreateRequest
from src.backend_ai.services.openai_responses_projection import project_response
from src.backend_ai.services.openai_responses_validation import to_runtime_input
from src.backend_ai.services.runtime_response_service import RuntimeResponseService
from src.shared.dtos.gateway import (
    AggregationResult,
    AggregationStatus,
    CostSummary,
    ExecutionResultTrace,
    GatewayRequest,
    OutputEvidence,
    PipelineResult,
    StageTrace,
    ValidationResult,
)
from src.shared.dtos.runtime_api import (
    RuntimeCost,
    RuntimeRequestResponse,
    RuntimeUsage,
    RuntimeUsageDetails,
    RuntimeUsageScope,
)


def _receipt(output: str = "hello", finish_reason: str = "stop") -> RuntimeRequestResponse:
    now = datetime(2026, 10, 7, tzinfo=UTC)
    return RuntimeRequestResponse(
        request_id="8a0e4dab-4766-4d0c-82f8-f18773dbe600",
        application_id="app-1",
        status="completed",
        execution_mode="live",
        output=output,
        models_used=["model-1"],
        providers_used=["openai"],
        usage=RuntimeUsage(
            execution=RuntimeUsageScope(input_tokens=49, output_tokens=5, total_tokens=54),
            pipeline=RuntimeUsageScope(input_tokens=56, output_tokens=7, total_tokens=63),
            complete=True,
        ),
        usage_details=RuntimeUsageDetails(
            cached_tokens=2, cache_write_tokens=3, reasoning_tokens=1, complete=True
        ),
        output_evidence=OutputEvidence(
            finish_reason=finish_reason,
            cached_tokens=2,
            cache_write_tokens=3,
            reasoning_tokens=1,
        ),
        cost=RuntimeCost(estimated_cost=0.0001),
        started_at=now,
        finished_at=now,
        latency_ms=1,
        config_version=1,
    )


def _payload(**overrides) -> ResponseCreateRequest:
    data = {"model": "auto", "input": "hello", "store": False}
    data.update(overrides)
    return ResponseCreateRequest.model_validate(data)


def test_projected_response_matches_pinned_sdk_and_has_stable_ids():
    first = project_response(_receipt(), _payload(metadata={"source": "test"}))
    replay = project_response(_receipt(), _payload(metadata={"source": "test"}))

    assert first.model_dump(mode="json") == replay.model_dump(mode="json")
    assert first.id == "resp_8a0e4dab47664d0c82f8f18773dbe600"
    assert first.output[0].id == "msg_8a0e4dab47664d0c82f8f18773dbe600_0"
    sdk_response = SDKResponse.model_validate(first.model_dump(mode="json"))
    assert sdk_response.output_text == "hello"
    assert sdk_response.usage.input_tokens == 49
    assert sdk_response.usage.output_tokens == 5
    assert sdk_response.usage.total_tokens == 54


def test_actual_projected_output_can_be_sent_back_as_assistant_history():
    response = project_response(_receipt(), _payload())
    history = response.output[0].model_dump(mode="json")
    payload = ResponseCreateRequest.model_validate({
        "model": "auto",
        "store": False,
        "input": [history, {"role": "user", "content": "next"}],
    })

    messages = to_runtime_input(payload).messages
    assert [message.role for message in messages] == ["assistant", "user"]
    assert messages[0].text() == "hello"


def test_public_response_model_uses_snapshot_name_and_safe_requested_model_fallback():
    mapped = project_response(_receipt(), _payload(), model_names={"model-1": "gpt-4.1-nano"})
    fallback = project_response(_receipt(), _payload())

    assert mapped.model == "gpt-4.1-nano"
    assert fallback.model == "auto"


@pytest.mark.parametrize(
    ("reason", "status", "detail"),
    [
        ("stop", "completed", None),
        ("length", "incomplete", "max_output_tokens"),
        ("content_filter", "incomplete", "content_filter"),
        ("unknown", "incomplete", None),
    ],
)
def test_finish_reason_controls_response_status(reason, status, detail):
    response = project_response(_receipt(finish_reason=reason), _payload())
    assert response.status == status
    assert (response.incomplete_details.reason if response.incomplete_details else None) == detail


def test_missing_detail_counters_means_usage_is_null():
    receipt = _receipt().model_copy(update={"usage_details": RuntimeUsageDetails(complete=False), "output_evidence": OutputEvidence(finish_reason="stop")})
    response = project_response(receipt, _payload())
    assert response.usage is None


def test_runtime_receipt_usage_is_inclusive_and_detail_counters_are_added_once():
    evidence = OutputEvidence(
        finish_reason="stop", cached_tokens=1, cache_write_tokens=2, reasoning_tokens=1
    )
    now = datetime(2026, 10, 7, tzinfo=UTC)
    result = PipelineResult.model_construct(
        request=GatewayRequest.model_construct(request_id="request-1", application_id="app-1", metadata={}),
        validation=ValidationResult.model_construct(accepted=True, errors=[]),
        execution_results=[ExecutionResultTrace(
            task_id="task-1", status="success", result="hello", executed_model="model-1",
            input_tokens=49, output_tokens=5, usage_complete=True, output_evidence=evidence,
        )],
        aggregation=AggregationResult(
            original_request_id="request-1", status=AggregationStatus.COMPLETE,
            final_answer="hello", strategy="none", reason="fixture", output_evidence=evidence,
        ),
        traces=[StageTrace(
            request_id="request-1", stage="understand", strategy="llm", model="internal-model",
            input_tokens=7, output_tokens=2, metadata={
                "usage_complete": True, "output_evidence": evidence.model_dump(mode="json")
            }, started_at=now, finished_at=now,
        )],
        costs=CostSummary(),
        output_evidence=evidence,
    )

    response = RuntimeResponseService.from_result(
        result, {}, wall_latency_ms=1, operation_started_at=now, finished_at=now, execution_mode="live"
    )

    assert response.usage.pipeline.input_tokens == 56
    assert response.usage.pipeline.output_tokens == 7
    assert response.usage.pipeline.total_tokens == 63
    assert response.usage_details.complete is True
    assert response.usage_details.cached_tokens == 2
    assert response.usage_details.cache_write_tokens == 4
    assert response.usage_details.reasoning_tokens == 2


def test_missing_old_receipt_finish_evidence_projects_incomplete():
    receipt = _receipt().model_copy(update={"output_evidence": None})
    response = project_response(receipt, _payload())
    assert response.status == "incomplete"
    assert response.incomplete_details is not None
    assert response.incomplete_details.reason is None
