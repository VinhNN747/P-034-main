from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_sdk_message_execution_prices_anthropic_cache_evidence(monkeypatch, streaming):
    from src.backend_ai.gateway.decomposition.no_decomposer import NoDecomposer
    from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
    from src.backend_ai.gateway.providers.base import ProviderResponse, ProviderStreamEvent
    from src.backend_ai.services import openai_message_execution as execution_module
    from src.backend_ai.services.openai_message_execution import execute_openai_messages
    from src.shared.core.pricing import ModelPricing, PricingRegistry
    from src.shared.dtos.gateway import OutputEvidence
    from src.shared.dtos.model_features import ModelPricingMetadata

    task = SimpleNamespace(task_id="task-1", task_type="classification", routing_signals=None, goal_ids=[])

    async def decompose(*_args):
        return SimpleNamespace(result=SimpleNamespace(subtasks=[task]))

    monkeypatch.setattr(NoDecomposer, "decompose", decompose)
    monkeypatch.setattr(RequestPipeline, "_attach_routing_signals",
        lambda *_args: SimpleNamespace(subtasks=[task]))
    evidence = OutputEvidence(cached_tokens=100_000, finish_reason="stop")
    response = ProviderResponse(text="answer", provider="anthropic", model_id="claude-haiku-5-5",
        input_tokens=100_001, output_tokens=1_000, usage_received=True, output_evidence=evidence)

    class Provider:
        async def generate(self, *_args, **_kwargs):
            return response

        async def stream(self, *_args, **_kwargs):
            yield ProviderStreamEvent(type="final", final=response)

    class Router:
        async def aroute(self, *_args, **_kwargs):
            return SimpleNamespace(selected_model="claude-haiku-5-5", provider_name="anthropic",
                provider_model="claude-haiku-5-5", reason="fixture", shadow=None)

    class Stream:
        def __init__(self):
            self.receipts = []

        def record_usage(self, value, **_kwargs):
            self.receipts.append(value)

        async def emit(self, _event):
            pass

    stream = Stream() if streaming else None
    if streaming:
        monkeypatch.setattr(execution_module, "current_stream", lambda: stream)
    pricing = PricingRegistry([ModelPricing(model_id="claude-haiku-5-5", provider="anthropic",
        input_price_per_million=0.1, output_price_per_million=0.5, pricing_version="fixture",
        pricing_rules=ModelPricingMetadata(standard_input_per_million=0.1,
            standard_output_per_million=0.5, long_context_input_threshold=100_000,
            long_context_input_per_million=0.5, long_context_output_per_million=2.5,
            cache_write_5m_input_per_million=0.125, cache_write_1h_input_per_million=0.2,
            cache_read_input_per_million=0.01))])
    prepared = SimpleNamespace(application_policy=SimpleNamespace(max_output_tokens=2_000),
        request_constraints=None, providers={"claude-haiku-5-5": Provider()},
        config=SimpleNamespace(timeout_seconds=10), pricing=pricing)
    result = SimpleNamespace(semantic_analysis=object(), profile=object(),
        effective_constraints=SimpleNamespace(constraints=SimpleNamespace(max_output_tokens=1_000,
            quality_priority="balanced")), goal_extraction=object(), attachments=None, decomposition=None,
        traces=[])
    payload = SimpleNamespace(messages=[SimpleNamespace(model_dump=lambda **_kwargs: {
        "role": "user", "content": "classify"})], tools=None, n=1, response_format=None,
        temperature=None, top_p=None, stop=None, seed=None, frequency_penalty=None,
        presence_penalty=None, parallel_tool_calls=None, tool_choice=None)
    request = SimpleNamespace(focus_text=lambda: "classify", request_id="request-1")

    await execute_openai_messages(request, result, payload, prepared, Router(), 10**9)

    assert result.execution_results[0].cost_usd == pytest.approx(0.0075005)
    assert stream is None or stream.receipts[0].output_evidence.cached_tokens == 100_000
