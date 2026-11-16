import pytest

from src.backend_ai.gateway.providers.base import (
    REASONING_TOKEN_HEADROOM,
    GenerationOptions,
    ProviderConfigurationError,
    ProviderOutputError,
    ProviderResponse,
)
from src.backend_ai.gateway.providers.gemini import GeminiProvider
from src.backend_ai.gateway.providers.offline import OfflineProvider
from src.backend_ai.gateway.providers.openai import OpenAIProvider


@pytest.mark.asyncio
async def test_offline_provider_is_stable_without_api_key():
    provider = OfflineProvider()

    first = await provider.generate(
        "hello",
        task_id="task-1",
        model_id="local-deterministic",
        timeout_seconds=1,
    )
    second = await provider.generate(
        "hello",
        task_id="task-1",
        model_id="local-deterministic",
        timeout_seconds=1,
    )

    assert isinstance(first, ProviderResponse)
    assert first == second
    assert first.text == "offline:local-deterministic:task-1"
    assert first.provider == "offline"
    assert first.input_tokens > 0
    assert first.output_tokens > 0
    assert first.usage_received is True


def test_openai_provider_instantiates_model_with_generation_cap(monkeypatch):
    import sys
    from types import ModuleType

    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = ModuleType("langchain_openai")
    module.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", module)
    provider = OpenAIProvider(enabled=True, api_key="local-test-key")

    provider._client_for("test-model", 4, 123)

    assert captured["max_tokens"] == 123


def test_openai_cap_maps_to_supported_completion_parameter():
    provider = OpenAIProvider(enabled=True, api_key="local-test-key")
    client = provider._client_for("gpt-4o-mini", 4, 123)
    assert client._default_params["max_completion_tokens"] == 123


@pytest.mark.asyncio
async def test_gemini_provider_sends_native_max_output_tokens(monkeypatch):
    captured = {}

    class FakeResponse:
        is_error = False

        def json(self):
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Tone: professional. Refining the output...", "thought": True},
                                {"text": "answer"},
                            ]
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2},
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, endpoint, *, headers, json):
            captured.update(endpoint=endpoint, headers=headers, json=json)
            return FakeResponse()

    monkeypatch.setattr("src.backend_ai.gateway.providers.gemini.httpx.AsyncClient", FakeAsyncClient)
    provider = GeminiProvider(api_key="test-key")
    result = await provider.generate(
        "hello",
        task_id="task-1",
        model_id="gemini-test",
        timeout_seconds=3,
        options=GenerationOptions(max_output_tokens=42),
    )

    # The answer budget plus thinking headroom; thought parts never reach the answer.
    assert captured["json"]["generationConfig"]["maxOutputTokens"] == 42 + REASONING_TOKEN_HEADROOM
    assert result.text == "answer"
    assert result.usage_received is True


def test_openai_provider_is_not_constructed_when_live_mode_is_disabled():
    with pytest.raises(ProviderConfigurationError, match="disabled"):
        OpenAIProvider(enabled=False)


def test_openai_provider_requires_a_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-env-key-must-be-ignored")

    with pytest.raises(ProviderConfigurationError, match="not supplied"):
        OpenAIProvider(enabled=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,thinking",
    [
        ("gemini-3.6-flash", {"thinkingLevel": "low"}),
        ("gemini-2.5-pro", {"thinkingBudget": 1024}),
    ],
)
async def test_gemini_evaluator_requests_json_and_excludes_thoughts(monkeypatch, model, thinking):
    import httpx

    captured = {}
    schema = {"type": "object", "properties": {"scores": {"type": "object"}}, "required": ["scores"]}

    def respond(request):
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {"thought": True, "text": "private reasoning"},
                                {"text": '{"scores":{}}'},
                            ]
                        },
                    }
                ],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 20},
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.backend_ai.gateway.providers.gemini.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    response = await GeminiProvider(api_key="test-key").generate(
        "Return JSON",
        task_id="evaluation",
        model_id=model,
        timeout_seconds=3,
        options=GenerationOptions(max_output_tokens=4096, json_schema=schema, low_reasoning=True),
    )
    assert captured["generationConfig"]["thinkingConfig"] == thinking
    assert captured["generationConfig"]["responseFormat"]["text"] == {"mimeType": "application/json", "schema": schema}
    assert response.text == '{"scores":{}}'
    assert response.output_tokens == 25


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [None, 0])
async def test_gemini_empty_thinking_output_keeps_billable_receipt(monkeypatch, candidate_count):
    import httpx

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}],
                "usageMetadata": {
                    "promptTokenCount": 77,
                    "thoughtsTokenCount": 1200,
                    **({"candidatesTokenCount": candidate_count} if candidate_count is not None else {}),
                },
            },
        )
    )
    monkeypatch.setattr(
        "src.backend_ai.gateway.providers.gemini.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    with pytest.raises(ProviderOutputError) as caught:
        await GeminiProvider(api_key="test-key").generate(
            "Score answers",
            task_id="evaluation",
            model_id="gemini-3.6-flash",
            timeout_seconds=3,
        )
    assert caught.value.code == "OUTPUT_TOKEN_LIMIT"
    assert caught.value.response.input_tokens == 77
    assert caught.value.response.output_tokens == 1200
    assert caught.value.response.usage_received is (candidate_count is not None)


@pytest.mark.asyncio
async def test_openai_evaluator_uses_json_mode_without_changing_reference_calls(monkeypatch):
    from types import SimpleNamespace

    calls = []

    class Client:
        def bind(self, **kwargs):
            calls.append(kwargs)
            return self

        async def ainvoke(self, prompt):
            return SimpleNamespace(
                content='{"scores":{}}', usage_metadata={}, response_metadata={"finish_reason": "stop"}
            )

    provider = OpenAIProvider(enabled=True, api_key="test-key")
    monkeypatch.setattr(provider, "_client_for", lambda *args, **kwargs: Client())
    await provider.generate(
        "Return JSON",
        task_id="evaluation",
        model_id="gpt-3.5-turbo",
        timeout_seconds=3,
        options=GenerationOptions(json_schema={"type": "object"}),
    )
    await provider.generate("Answer directly", task_id="reference", model_id="gpt-3.5-turbo", timeout_seconds=3)
    assert calls == [{"response_format": {"type": "json_object"}}]
