from datetime import UTC, datetime

from src.backend_ai.dtos.openai_responses import ResponseCreateRequest
from src.backend_ai.services.openai_responses_projection import project_response
from src.shared.dtos.gateway import OutputEvidence
from src.shared.dtos.runtime_api import RuntimeRequestResponse, RuntimeUsage, RuntimeUsageScope


def test_core_measured_totals_survive_missing_usage_breakdown():
    receipt = RuntimeRequestResponse(
        request_id="usage-probe",
        application_id="app",
        status="completed",
        execution_mode="live",
        output="text",
        config_version=1,
        started_at=datetime.now(UTC),
        usage=RuntimeUsage(
            execution=RuntimeUsageScope(input_tokens=46, output_tokens=4, total_tokens=50), complete=True
        ),
        output_evidence=OutputEvidence(finish_reason="stop"),
    )
    response = project_response(receipt, ResponseCreateRequest(model="auto", input="hello", store=False))
    assert response.gateway_usage.input_tokens == 46
    assert response.gateway_usage.output_tokens == 4
    assert response.gateway_usage.total_tokens == 50
    assert response.gateway_usage.cached_tokens is None
    assert response.usage is None  # strict SDK cannot represent unknown required counters


def test_measured_final_usage_survives_unknown_internal_usage():
    from src.backend_ai.routers.openai_compat import _usage
    receipt = RuntimeRequestResponse(request_id="known-final", application_id="app", status="completed",
        execution_mode="live", output="text", config_version=1, started_at=datetime.now(UTC),
        usage=RuntimeUsage(final_output=RuntimeUsageScope(input_tokens=3, output_tokens=2, total_tokens=5),
            complete=False))
    assert _usage(receipt).total_tokens == 5


def test_known_detail_counter_survives_other_unknown_counters():
    from types import SimpleNamespace

    from src.backend_ai.services.runtime_response_service import RuntimeResponseService
    details = RuntimeResponseService._usage_details(SimpleNamespace(traces=[]), [SimpleNamespace(
        status='success', output_evidence=OutputEvidence(cached_tokens=0, cache_write_tokens=None, reasoning_tokens=None))])
    assert details.cached_tokens == 0
    assert details.reasoning_tokens is None and details.cache_write_tokens is None and not details.complete


def test_unknown_intermediate_usage_does_not_hide_known_terminal_producer():
    from types import SimpleNamespace

    from src.backend_ai.services.runtime_response_service import RuntimeResponseService
    result = SimpleNamespace(traces=[], aggregation=SimpleNamespace(contributions=[
        SimpleNamespace(task_id='final', used_in_final_answer=True),
        SimpleNamespace(task_id='internal', used_in_final_answer=False)]))
    executions = [SimpleNamespace(task_id=task, usage_complete=known, final_usage_complete=known,
        final_input_tokens=None, final_output_tokens=None, input_tokens=3, output_tokens=2)
        for task, known in [('internal', False), ('final', True)]]
    assert RuntimeResponseService._final_usage(result, executions).total_tokens == 5


def test_unknown_aggregate_usage_does_not_fall_back_to_subtask_usage():
    from types import SimpleNamespace

    from src.backend_ai.services.runtime_response_service import RuntimeResponseService
    result = SimpleNamespace(traces=[SimpleNamespace(stage='aggregate',model='model',status='success',
        metadata={'usage_complete':False})],aggregation=None)
    execution = SimpleNamespace(task_id='task',usage_complete=True,final_usage_complete=True,
        final_input_tokens=3,final_output_tokens=2)
    assert RuntimeResponseService._final_usage(result,[execution]) is None


def test_responses_unknown_final_usage_does_not_publish_execution_as_final():
    receipt = RuntimeRequestResponse(request_id='unknown-final',application_id='app',status='completed',
        execution_mode='live',output='text',config_version=1,started_at=datetime.now(UTC),
        output_evidence=OutputEvidence(finish_reason='stop'),
        usage=RuntimeUsage(execution=RuntimeUsageScope(input_tokens=3,output_tokens=2,total_tokens=5),complete=False))
    response = project_response(receipt,ResponseCreateRequest(model='auto',input='hello',store=False))
    assert response.gateway_usage is None and response.usage is None
