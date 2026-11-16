import asyncio

import pytest
from langchain_core.messages import AIMessageChunk

from src.backend_ai.gateway.providers.openai import OpenAIProvider


def test_named_function_stop_is_normalized_without_overriding_truncation():
    from time import perf_counter
    from types import SimpleNamespace

    from src.backend_ai.gateway.providers.openai_messages import normalized_response

    call = {"id": "call_fixture", "type": "function",
            "function": {"name": "weather", "arguments": '{"city":"Hanoi"}'}}
    for upstream_reason, expected in (("stop", "tool_calls"), ("length", "length")):
        response = normalized_response([{"index": 0, "finish_reason": upstream_reason,
            "message": {"role": "assistant", "content": None, "tool_calls": [call]}}],
            SimpleNamespace(prompt_tokens=3, completion_tokens=2),
            model_id="gpt-4o-mini", task_id="task", started=perf_counter())
        assert response.output_evidence.finish_reason == expected
        assert response.metadata["openai_choices"][0]["finish_reason"] == expected
        assert response.metadata["openai_choices"][0]["message"]["tool_calls"] == [call]


@pytest.mark.asyncio
async def test_openai_stream_delivers_whitespace_and_delta_before_provider_finishes(monkeypatch):
    finished = asyncio.Event()

    class Client:
        async def astream(self, prompt, **kwargs):
            assert kwargs["stream_usage"] is True
            yield AIMessageChunk(content=" hello")
            await asyncio.sleep(0.05)
            yield AIMessageChunk(content=" world ", response_metadata={"finish_reason": "stop"},
                usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
            finished.set()

    provider = OpenAIProvider(api_key="fixture", enabled=True)
    monkeypatch.setattr(provider, "_client_for", lambda *args, **kwargs: Client())
    stream = provider.stream("prompt", task_id="task", model_id="gpt-4o-mini", timeout_seconds=1)
    first = await anext(stream)
    assert first.type == "text_delta" and first.text == " hello"
    assert not finished.is_set()
    rest = [event async for event in stream]
    assert [event.text for event in rest if event.type == 'text_delta'] == [" world "]
    assert any(event.type == 'usage' and event.final.usage_received for event in rest)
    assert rest[-1].type == "final"
    assert rest[-1].final.text == " hello world "
    assert rest[-1].final.input_tokens == 3
    assert rest[-1].final.output_evidence.finish_reason == "stop"


@pytest.mark.asyncio
async def test_closing_stream_closes_upstream_iterator(monkeypatch):
    closed = asyncio.Event()

    class Client:
        async def astream(self, prompt, **kwargs):
            try:
                yield AIMessageChunk(content="first")
                await asyncio.Event().wait()
            finally:
                closed.set()

    provider = OpenAIProvider(api_key="fixture", enabled=True)
    monkeypatch.setattr(provider, "_client_for", lambda *args, **kwargs: Client())
    stream = provider.stream("prompt", task_id="task", model_id="fixture", timeout_seconds=1)
    await anext(stream)
    await stream.aclose()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_anthropic_stream_hides_thinking_normalizes_tool_json_and_closes_on_cancel():
    import asyncio
    from types import SimpleNamespace

    from src.backend_ai.gateway.providers.anthropic import AnthropicProvider
    from src.backend_ai.gateway.providers.base import GenerationOptions

    closed = asyncio.Event()

    def event(kind, **values):
        return SimpleNamespace(type=kind, **values)

    class Stream:
        def __init__(self):
            self.events = [
                event("message_start", message=SimpleNamespace(usage=SimpleNamespace(input_tokens=8,
                    cache_read_input_tokens=2, cache_creation_input_tokens=5,
                    cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=2,
                        ephemeral_1h_input_tokens=3)))),
                event("content_block_start", index=0, content_block=SimpleNamespace(type="thinking",
                    thinking="secret thought", signature="secret signature")),
                event("content_block_delta", index=0, delta=SimpleNamespace(type="thinking_delta", thinking="secret")),
                event("content_block_start", index=1, content_block=SimpleNamespace(type="tool_use", id="call-2",
                    name="weather", input={})),
                event("content_block_delta", index=1, delta=SimpleNamespace(type="input_json_delta",
                    partial_json='{"city":')),
                event("content_block_delta", index=1, delta=SimpleNamespace(type="input_json_delta",
                    partial_json='"Hanoi"}')),
                event("content_block_start", index=2, content_block=SimpleNamespace(type="tool_use", id="call-3",
                    name="weather", input={})),
                event("content_block_delta", index=2, delta=SimpleNamespace(type="input_json_delta",
                    partial_json='{"city":"Hue"}')),
                event("content_block_start", index=3, content_block=SimpleNamespace(type="text", text="Hi")),
                event("content_block_delta", index=3, delta=SimpleNamespace(type="text_delta", text=" there")),
                event("message_delta", delta=SimpleNamespace(stop_reason="tool_use"),
                    usage=SimpleNamespace(output_tokens=4)),
                event("message_stop"),
            ]

        def __aiter__(self):
            async def generate():
                for item in self.events:
                    yield item
                    await asyncio.sleep(0)
            return generate()

        async def close(self):
            closed.set()

    class Messages:
        async def create(self, **kwargs):
            assert kwargs["stream"] is True
            return Stream()

    profile = {"max_output_tokens": 1000, "feature_metadata_json": {"constraints": {
        "thinking_mode": "adaptive", "default_effort": "medium", "forced_tool_choice": "conditional"}}}
    provider = AnthropicProvider(api_key="fixture", client=SimpleNamespace(messages=Messages()),
        model_profiles={"claude-haiku-5-5": profile})
    events = [event async for event in provider.stream("ignored", task_id="t1", model_id="claude-haiku-5-5",
        timeout_seconds=3, options=GenerationOptions(messages=({"role": "user", "content": "hi"},)))]
    assert [item.text for item in events if item.type == "text_delta"] == ["Hi", " there"]
    final_events = [item for item in events if item.type == "final"]
    assert len(final_events) == 1
    final = final_events[0].final
    assert final.text == "Hi there"
    assert final.input_tokens == 15 and final.output_tokens == 4
    assert final.output_evidence.cached_tokens == 2
    assert final.output_evidence.cache_write_tokens == 5
    assert final.output_evidence.cache_write_5m_tokens == 2
    assert final.output_evidence.cache_write_1h_tokens == 3
    assert final.output_evidence.finish_reason == "tool_calls"
    calls = final.metadata["openai_choices"][0]["message"]["tool_calls"]
    assert [call["id"] for call in calls] == ["call-2", "call-3"]
    assert [call["function"]["arguments"] for call in calls] == ['{"city":"Hanoi"}', '{"city":"Hue"}']
    tool_deltas = [item for item in events if item.type == "tool_delta"]
    assert [item.index for item in tool_deltas if item.tool_delta and item.tool_delta.get("id")] == [0, 1]
    assert "secret" not in str(final.metadata)
    assert closed.is_set()


@pytest.mark.asyncio
async def test_closing_anthropic_stream_closes_sdk_stream():
    import asyncio
    from types import SimpleNamespace

    from src.backend_ai.gateway.providers.anthropic import AnthropicProvider
    from src.backend_ai.gateway.providers.base import GenerationOptions

    closed = asyncio.Event()

    class Stream:
        def __aiter__(self):
            async def generate():
                yield SimpleNamespace(type="content_block_delta", index=0,
                    delta=SimpleNamespace(type="text_delta", text="first"))
                await asyncio.Event().wait()
            return generate()

        async def close(self):
            closed.set()

    class Messages:
        async def create(self, **kwargs):
            return Stream()

    profile = {"max_output_tokens": 1000, "feature_metadata_json": {"constraints": {
        "thinking_mode": "adaptive", "default_effort": "medium", "forced_tool_choice": "conditional"}}}
    provider = AnthropicProvider(api_key="fixture", client=SimpleNamespace(messages=Messages()),
        model_profiles={"claude-haiku-5-5": profile})
    stream = provider.stream("ignored", task_id="t1", model_id="claude-haiku-5-5", timeout_seconds=3,
        options=GenerationOptions(messages=({"role": "user", "content": "hi"},)))
    first = await anext(stream)
    assert first.type == "text_delta"
    await stream.aclose()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_anthropic_stream_publishes_observed_usage_before_upstream_timeout():
    from types import SimpleNamespace

    from src.backend_ai.gateway.providers.anthropic import AnthropicProvider
    from src.backend_ai.gateway.providers.base import TransientProviderError

    def event(kind, **values):
        return SimpleNamespace(type=kind, **values)

    class Stream:
        def __aiter__(self):
            async def generate():
                yield event("message_start", message=SimpleNamespace(usage=SimpleNamespace(input_tokens=80,
                    cache_read_input_tokens=20, cache_creation_input_tokens=0)))
                yield event("message_delta", delta=SimpleNamespace(stop_reason=None),
                    usage=SimpleNamespace(output_tokens=7))
                raise TimeoutError("upstream disconnected")
            return generate()

        async def close(self):
            pass

    class Messages:
        async def create(self, **kwargs):
            return Stream()

    provider = AnthropicProvider(api_key="fixture", client=SimpleNamespace(messages=Messages()),
        model_profiles={"claude-haiku-5-5": {"max_output_tokens": 1000, "feature_metadata_json": {
            "constraints": {"thinking_mode": "adaptive", "default_effort": "medium"}}}})
    observed = []
    with pytest.raises(TransientProviderError):
        async for item in provider.stream("ignored", task_id="t1", model_id="claude-haiku-5-5",
            timeout_seconds=3):
            if item.final is not None:
                observed.append(item.final)

    assert observed
    assert observed[-1].input_tokens == 100 and observed[-1].output_tokens == 7
    assert observed[-1].usage_received is True


@pytest.mark.asyncio
async def test_anthropic_stream_rejects_eof_without_message_stop_and_keeps_usage():
    from types import SimpleNamespace

    from src.backend_ai.gateway.providers.anthropic import AnthropicProvider
    from src.backend_ai.gateway.providers.base import ProviderOutputError

    class Stream:
        def __aiter__(self):
            async def generate():
                yield SimpleNamespace(type="message_start", message=SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=8)))
                yield SimpleNamespace(type="content_block_delta", index=0,
                    delta=SimpleNamespace(type="text_delta", text="partial"))
            return generate()

        async def close(self):
            pass

    class Messages:
        async def create(self, **kwargs):
            return Stream()

    provider = AnthropicProvider(api_key="fixture", client=SimpleNamespace(messages=Messages()),
        model_profiles={"claude-haiku-5-5": {"max_output_tokens": 1000, "feature_metadata_json": {
            "constraints": {"thinking_mode": "adaptive", "default_effort": "medium"}}}})
    observed = []
    with pytest.raises(ProviderOutputError) as failure:
        async for item in provider.stream("ignored", task_id="t1", model_id="claude-haiku-5-5",
            timeout_seconds=3):
            if item.final is not None:
                observed.append(item.final)

    assert observed and observed[-1].input_tokens == 8
    assert observed[-1].usage_received is False
    assert failure.value.response.input_tokens == 8


@pytest.mark.asyncio
async def test_anthropic_stream_publishes_input_usage_before_consumer_cancellation():
    import asyncio
    from types import SimpleNamespace

    from src.backend_ai.gateway.providers.anthropic import AnthropicProvider

    waiting = asyncio.Event()
    closed = asyncio.Event()

    class Stream:
        def __aiter__(self):
            async def generate():
                yield SimpleNamespace(type="message_start", message=SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=9, cache_read_input_tokens=3)))
                waiting.set()
                await asyncio.Event().wait()
            return generate()

        async def close(self):
            closed.set()

    class Messages:
        async def create(self, **kwargs):
            return Stream()

    provider = AnthropicProvider(api_key="fixture", client=SimpleNamespace(messages=Messages()),
        model_profiles={"claude-haiku-5-5": {"max_output_tokens": 1000, "feature_metadata_json": {
            "constraints": {"thinking_mode": "adaptive", "default_effort": "medium"}}}})
    iterator = provider.stream("ignored", task_id="t1", model_id="claude-haiku-5-5", timeout_seconds=3).__aiter__()
    observed = await anext(iterator)
    assert observed.type == "usage" and observed.final.input_tokens == 12
    blocked = asyncio.create_task(anext(iterator))
    await waiting.wait()
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked
    await iterator.aclose()

    assert closed.is_set()
