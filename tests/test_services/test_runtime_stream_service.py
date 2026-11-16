import asyncio
import json
from types import SimpleNamespace

import pytest
from openai.types.responses import Response as SDKResponse
from openai.types.responses import ResponseErrorEvent

from src.backend_ai.dtos.openai_responses import ResponseCreateRequest
from src.backend_ai.services.openai_responses_live_stream import ResponseStreamEncoder
from src.backend_ai.services.openai_responses_stream import encode_response_event


def test_response_error_event_uses_official_error_type():
    value = {'type':'error','sequence_number':0,'code':'stream_error', 'message':'Stream failed.', 'param':None}
    wire = encode_response_event(value)
    assert wire.startswith('event: error\n')
    assert ResponseErrorEvent.model_validate(json.loads(wire.split('data: ',1)[1])).type == 'error'


def test_failed_response_event_has_strict_sdk_response_fields():
    encoder = ResponseStreamEncoder(SimpleNamespace(request_id='fixture',model='model'),
                                    ResponseCreateRequest(model='auto',input='hello'))
    wire = encoder.failure('stream_error', 'Stream failed.')
    event = json.loads(wire.split('data: ',1)[1])
    failed = SDKResponse.model_validate(event['response'])
    assert event['type'] == 'response.failed' and failed.status == 'failed'
    assert failed.error.code == 'server_error' and failed.gateway_error_code == 'stream_error'


@pytest.mark.asyncio
async def test_slow_consumer_bounds_queue_and_cancels_owner_once():
    from src.backend_ai.gateway.providers.base import ProviderStreamEvent
    from src.backend_ai.services.runtime_stream_service import RuntimeStreamSession
    stream = RuntimeStreamSession()
    cancelled = []
    async def owner():
        try:
            for _ in range(1000):
                await stream.emit(ProviderStreamEvent(type='text_delta',text='x'*65536))
        finally:
            cancelled.append(True)
    stream.start(owner)
    await stream.prime()
    await asyncio.sleep(0.05)
    assert stream.queue.qsize() == 64 and not stream.task.done()
    await stream.aclose()
    await stream.aclose()
    assert stream.task.done() and cancelled == [True]


def test_record_usage_keeps_observed_partial_counters_and_marks_cost_estimated():
    from src.backend_ai.gateway.providers.base import ProviderResponse
    from src.backend_ai.services.runtime_stream_service import RuntimeStreamSession
    from src.shared.core.pricing import ModelPricing, PricingRegistry

    stream = RuntimeStreamSession()
    stream.partial_result = SimpleNamespace(request=SimpleNamespace(request_id="request-1"), traces=[],
        execution_results=[])
    stream.pricing = PricingRegistry([ModelPricing(model_id="claude-haiku-5-5", provider="anthropic",
        input_price_per_million=1, output_price_per_million=5, pricing_version="fixture")])
    observed = ProviderResponse(text="", provider="anthropic", model_id="claude-haiku-5-5", input_tokens=80,
        output_tokens=0, usage_received=False,
        metadata={"anthropic_usage": {"input_tokens": 60, "cache_read_input_tokens": 20}})

    stream.record_usage(observed, task_id="task-1")

    assert len(stream.partial_result.traces) == 1
    assert stream.partial_result.traces[0].input_tokens == 80
    assert stream.partial_result.traces[0].metadata["cost_estimated"] is True
    assert len(stream.partial_result.execution_results) == 1
    assert stream.partial_result.execution_results[0].input_tokens == 80
    assert stream.partial_result.execution_results[0].usage_complete is False

    complete = ProviderResponse(text="partial answer", provider="anthropic", model_id="claude-haiku-5-5",
        input_tokens=90, output_tokens=2, usage_received=True)
    stream.record_usage(complete, task_id="task-1")

    assert len(stream.partial_result.traces) == 1
    assert stream.partial_result.traces[0].input_tokens == 90
    assert stream.partial_result.traces[0].output_tokens == 2
    assert stream.partial_result.traces[0].cost_usd == pytest.approx(0.0001)
    assert len(stream.partial_result.execution_results) == 1
