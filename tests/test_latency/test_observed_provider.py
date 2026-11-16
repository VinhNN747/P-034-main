import asyncio
from datetime import UTC, datetime

import pytest

from src.backend_ai.gateway.providers.base import (
    ProviderClient,
    ProviderResponse,
    ProviderStreamEvent,
)
from src.backend_ai.gateway.providers.latency_observed import LatencyObservedProvider
from src.backend_ai.latency.context import LatencyRequestContext, latency_request_scope
from src.backend_ai.latency.contracts import HistorySnapshot, LatencyConfig, LatencyFeatures, ModelKey
from src.backend_ai.latency.telemetry import LatencyCollector

KEY_A = ModelKey("model-a", "provider", "upstream/a", "fingerprint", "openai")
KEY_B = ModelKey("model-b", "provider", "upstream/b", "fingerprint", "openai")
NOW = datetime(2026, 10, 14, tzinfo=UTC)


class FakeProvider(ProviderClient):
    name = "fake"

    def __init__(
        self,
        *,
        fail_model: str | None = None,
        error_model: str | None = None,
        blocked_model: str | None = None,
    ):
        self.fail_model = fail_model
        self.error_model = error_model
        self.blocked_model = blocked_model
        self.error = ValueError("adapter rejected request")
        self.response = ProviderResponse("ok", self.name, "model-b", latency_ms=10.0)

    async def generate(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
        await asyncio.sleep(0.02 if model_id == self.fail_model else 0.003)
        if model_id == self.fail_model:
            raise TimeoutError("provider timeout")
        if model_id == self.error_model:
            raise self.error
        if model_id == self.blocked_model:
            await asyncio.Event().wait()
        return self.response

    async def stream(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
        yield ProviderStreamEvent("usage")
        await asyncio.sleep(0.005)
        yield ProviderStreamEvent("text_delta", text="hello")
        yield ProviderStreamEvent("final", final=ProviderResponse("hello", self.name, model_id, latency_ms=20.0))


def _scope(collector: LatencyCollector):
    context = LatencyRequestContext(
        request_id="request", source="openai_chat", mode="observe", artifact=None,
        history=HistorySnapshot(NOW, {}), collector=collector, bindings={"model-a": KEY_A, "model-b": KEY_B},
    )
    context.bind_features("task", LatencyFeatures(KEY_A, "profile-a", NOW, {"input_proxy": 10}))
    return context


@pytest.mark.asyncio
async def test_fallback_attempts_keep_separate_model_identity_and_elapsed_time():
    collector = LatencyCollector(LatencyConfig())
    wrapper = LatencyObservedProvider(FakeProvider(fail_model="model-a"), bindings={"model-a": KEY_A, "model-b": KEY_B}, collector=collector)
    context = _scope(collector)

    with latency_request_scope(context):
        with pytest.raises(TimeoutError):
            await wrapper.generate("prompt", task_id="task", model_id="model-a", timeout_seconds=1)
        response = await wrapper.generate("prompt", task_id="task", model_id="model-b", timeout_seconds=1)

    rows = [collector.queue.get_nowait(), collector.queue.get_nowait()]
    assert response.model_id == "model-b"
    assert rows[0].status == "timeout" and rows[0].features.model_key == KEY_A
    assert rows[1].status == "success" and rows[1].features.model_key == KEY_B
    assert rows[1].provider_total_ms < rows[0].provider_total_ms
    assert response is wrapper.inner.response


@pytest.mark.asyncio
async def test_error_and_cancellation_are_recorded_and_original_exception_survives():
    collector = LatencyCollector(LatencyConfig())
    inner = FakeProvider(error_model="model-a", blocked_model="model-b")
    wrapper = LatencyObservedProvider(inner, bindings={"model-a": KEY_A, "model-b": KEY_B}, collector=collector)
    context = _scope(collector)

    with latency_request_scope(context):
        with pytest.raises(ValueError) as raised:
            await wrapper.generate("prompt", task_id="task", model_id="model-a", timeout_seconds=1)
        task = asyncio.create_task(wrapper.generate("prompt", task_id="task", model_id="model-b", timeout_seconds=1))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert raised.value is inner.error
    error = collector.queue.get_nowait()
    cancelled = collector.queue.get_nowait()
    assert error.status == "error" and error.error_category == "ValueError"
    assert cancelled.status == "cancelled" and cancelled.provider_total_ms is not None


@pytest.mark.asyncio
async def test_stream_preserves_events_and_ignores_usage_for_ttft():
    collector = LatencyCollector(LatencyConfig())
    wrapper = LatencyObservedProvider(FakeProvider(), bindings={"model-a": KEY_A}, collector=collector)
    context = _scope(collector)

    with latency_request_scope(context):
        events = [event async for event in wrapper.stream("prompt", task_id="task", model_id="model-a", timeout_seconds=1)]

    observation = collector.queue.get_nowait()
    assert [event.type for event in events] == ["usage", "text_delta", "final"]
    assert observation.status == "success"
    assert observation.ttft_ms is not None and observation.ttft_ms >= 4
    assert observation.consumer_pause_ms < 100


@pytest.mark.asyncio
async def test_slow_stream_consumer_is_recorded_but_training_ineligible():
    collector = LatencyCollector(LatencyConfig())
    wrapper = LatencyObservedProvider(FakeProvider(), bindings={"model-a": KEY_A}, collector=collector)
    context = _scope(collector)
    with latency_request_scope(context):
        iterator = wrapper.stream("prompt", task_id="task", model_id="model-a", timeout_seconds=1)
        first = await anext(iterator)
        assert first.type == "usage"
        await asyncio.sleep(0.12)
        _ = [event async for event in iterator]

    observation = collector.queue.get_nowait()
    assert observation.consumer_pause_ms >= 100
    assert observation.training_eligible is False


@pytest.mark.asyncio
async def test_anthropic_model_key_records_total_time_and_stream_ttft_with_catalog_identity():
    key = ModelKey("catalog-anthropic", "anthropic-connection", "claude-haiku-5-5", "anthropic-fingerprint",
        "anthropic")
    collector = LatencyCollector(LatencyConfig())
    wrapper = LatencyObservedProvider(FakeProvider(), bindings={"claude-haiku-5-5": key}, collector=collector)
    context = LatencyRequestContext("anthropic-request", "openai_chat", "observe", None,
        HistorySnapshot(NOW, {}), collector, {"claude-haiku-5-5": key})
    context.bind_features("task", LatencyFeatures(key, "anthropic-profile", NOW, {"input_proxy": 10}))

    with latency_request_scope(context):
        response = await wrapper.generate("short prompt", task_id="task", model_id="claude-haiku-5-5",
            timeout_seconds=1)
        events = [event async for event in wrapper.stream("short prompt", task_id="task",
            model_id="claude-haiku-5-5", timeout_seconds=1)]

    generated = collector.queue.get_nowait()
    streamed = collector.queue.get_nowait()
    assert response.model_id == "model-b"
    assert generated.features.model_key.provider_kind == "anthropic"
    assert streamed.features.model_key.catalog_model_id == "catalog-anthropic"
    assert streamed.ttft_ms is not None and streamed.provider_total_ms >= streamed.ttft_ms
    assert events[-1].type == "final"
