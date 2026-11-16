import asyncio
from types import SimpleNamespace

import pytest

from src.backend_ai.gateway.providers.anthropic import AnthropicProvider
from src.backend_ai.gateway.providers.base import (
    GenerationOptions,
    ModelUnavailableError,
    PermanentProviderError,
    ProviderConfigurationError,
    TransientProviderError,
)

PROFILE = {"max_output_tokens": 128_000, "feature_metadata_json": {"constraints": {
    "thinking_mode": "adaptive", "default_effort": "medium", "forced_tool_choice": "conditional"}}}


class FakeMessages:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return self.value

    async def count_tokens(self, **kwargs):
        self.requests.append({"count_tokens": kwargs})
        return SimpleNamespace(input_tokens=42)


class FakeClient:
    def __init__(self, value=None, error=None):
        self.messages = FakeMessages(value, error)


def _response():
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="Hello")], stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=4, output_tokens=2))


@pytest.mark.asyncio
async def test_generate_uses_official_sdk_request_and_returns_visible_text_and_usage():
    client = FakeClient(_response())
    provider = AnthropicProvider(api_key="fixture", client=client,
        model_profiles={"claude-haiku-5-5": PROFILE})

    result = await provider.generate("ignored", task_id="t1", model_id="claude-haiku-5-5", timeout_seconds=3,
        options=GenerationOptions(messages=({"role": "user", "content": "Hello"},), max_output_tokens=500))

    request = client.messages.requests[0]
    assert request["stream"] is False
    assert request["model"] == "claude-haiku-5-5"
    assert request["max_tokens"] == 500
    assert request["thinking"] == {"type": "adaptive"}
    assert result.text == "Hello"
    assert result.input_tokens == 4 and result.output_tokens == 2 and result.usage_received


@pytest.mark.asyncio
async def test_count_tokens_uses_native_message_profile_without_generation_fields():
    client = FakeClient(_response())
    provider = AnthropicProvider(api_key="fixture", client=client,
        model_profiles={"claude-haiku-5-5": PROFILE})

    count = await provider.count_tokens(
        "ignored", task_id="t1", model_id="claude-haiku-5-5", timeout_seconds=3,
        options=GenerationOptions(messages=({"role": "user", "content": "Hello"},), max_output_tokens=500),
    )

    assert count == 42
    request = client.messages.requests[0]["count_tokens"]
    assert request == {"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Hello"}]}],
        "thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}


@pytest.mark.asyncio
async def test_native_sdk_client_has_per_request_timeout_and_retries_disabled(monkeypatch):
    import sys
    from types import ModuleType

    captured = {}

    class NativeClient(FakeClient):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(_response())

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    module = ModuleType("anthropic")
    module.AsyncAnthropic = NativeClient
    monkeypatch.setitem(sys.modules, "anthropic", module)
    provider = AnthropicProvider(api_key="fixture", base_url="https://api.anthropic.com",
        model_profiles={"claude-haiku-5-5": PROFILE})

    await provider.generate("ignored", task_id="t1", model_id="claude-haiku-5-5", timeout_seconds=7,
        options=GenerationOptions(messages=({"role": "user", "content": "Hello"},)))

    assert captured == {"api_key": "fixture", "base_url": "https://api.anthropic.com", "timeout": 7,
        "max_retries": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("status, error_type", [
    (401, PermanentProviderError), (403, PermanentProviderError), (404, ModelUnavailableError),
    (429, TransientProviderError), (529, TransientProviderError),
])
async def test_generate_maps_anthropic_status_to_provider_policy(status, error_type):
    error = RuntimeError("upstream error")
    error.status_code = status
    provider = AnthropicProvider(api_key="fixture", client=FakeClient(error=error),
        model_profiles={"claude-haiku-5-5": PROFILE})

    with pytest.raises(error_type) as caught:
        await provider.generate("x", task_id="t1", model_id="claude-haiku-5-5", timeout_seconds=3)
    assert caught.value.status_code == status


@pytest.mark.asyncio
async def test_generate_maps_timeout_to_transient_and_cancellation_propagates():
    provider = AnthropicProvider(api_key="fixture", client=FakeClient(error=TimeoutError()),
        model_profiles={"claude-haiku-5-5": PROFILE})
    with pytest.raises(TransientProviderError):
        await provider.generate("x", task_id="t1", model_id="claude-haiku-5-5", timeout_seconds=3)

    class CancelMessages:
        async def create(self, **kwargs):
            raise asyncio.CancelledError()
    provider = AnthropicProvider(api_key="fixture", client=SimpleNamespace(messages=CancelMessages()),
        model_profiles={"claude-haiku-5-5": PROFILE})
    with pytest.raises(asyncio.CancelledError):
        await provider.generate("x", task_id="t1", model_id="claude-haiku-5-5", timeout_seconds=3)


def test_provider_requires_enabled_flag_key_and_database_model_profile():
    with pytest.raises(ProviderConfigurationError, match="disabled"):
        AnthropicProvider(api_key="fixture", enabled=False)
    with pytest.raises(ProviderConfigurationError, match="credential"):
        AnthropicProvider(api_key=" ")


@pytest.mark.parametrize("kwargs, supported", [
    ({"temperature": 0}, False), ({"top_p": 0.5}, False), ({"seed": 7}, False),
    ({"frequency_penalty": 0.1}, False), ({"presence_penalty": 0.1}, False),
    ({"stop": ["END"]}, True), ({}, True),
])
def test_anthropic_request_preflight_rejects_custom_sampling_before_routing(kwargs, supported):
    from src.backend_ai.services.openai_message_execution import preflight_options
    from src.shared.dtos.gateway import RequestConstraints
    from src.shared.dtos.runtime_api import RuntimeAPIError

    model = SimpleNamespace(id="model-id", name="claude-haiku-5-5", capabilities_json=["tool_use",
        "structured_output"], feature_metadata_json=PROFILE["feature_metadata_json"])
    provider = SimpleNamespace(id="provider-id", name="anthropic", kind="anthropic")
    row = ("candidate", model, provider)
    prepared = SimpleNamespace(application_policy=RequestConstraints(), request_constraints=RequestConstraints(),
        routing_snapshot=SimpleNamespace(candidates=(row,)))
    payload_values = {key: None for key in ("temperature", "top_p", "stop", "seed", "frequency_penalty",
        "presence_penalty", "parallel_tool_calls", "tool_choice")}
    payload_values.update(kwargs, tools=None, n=1, messages=[], response_format=None)
    payload = SimpleNamespace(**payload_values)

    if supported:
        assert preflight_options(payload, prepared) == (row,)
    else:
        with pytest.raises(RuntimeAPIError) as caught:
            preflight_options(payload, prepared)
        assert caught.value.status_code == 400
        assert caught.value.code == "UNSUPPORTED_PARAMETER"


def test_anthropic_preflight_rejects_unsupported_structured_output_before_routing():
    from src.backend_ai.services.openai_message_execution import preflight_options
    from src.shared.dtos.gateway import RequestConstraints
    from src.shared.dtos.runtime_api import RuntimeAPIError

    model = SimpleNamespace(id="model-id", name="claude-haiku-5-5", capabilities_json=["structured_output"],
        feature_metadata_json=PROFILE["feature_metadata_json"])
    provider = SimpleNamespace(id="provider-id", name="anthropic", kind="anthropic")
    row = ("candidate", model, provider)
    prepared = SimpleNamespace(application_policy=RequestConstraints(), request_constraints=RequestConstraints(),
        routing_snapshot=SimpleNamespace(candidates=(row,)))
    payload = SimpleNamespace(tools=None, n=1, messages=[], response_format={"type": "json_schema",
        "json_schema": {"name": "Answer", "schema": {"type": "object", "properties": {
            "score": {"type": "number", "minimum": 0}}, "required": ["score"], "additionalProperties": False}}},
        **{key: None for key in ("temperature", "top_p", "stop", "seed", "frequency_penalty",
            "presence_penalty", "parallel_tool_calls", "tool_choice")})

    with pytest.raises(RuntimeAPIError) as caught:
        preflight_options(payload, prepared)

    assert caught.value.status_code == 400
    assert caught.value.details["param"] == "response_format"


def test_anthropic_preflight_rejects_unsupported_strict_tool_schema_before_routing():
    from src.backend_ai.services.openai_message_execution import preflight_options
    from src.shared.dtos.gateway import RequestConstraints
    from src.shared.dtos.runtime_api import RuntimeAPIError

    model = SimpleNamespace(id="model-id", name="claude-haiku-5-5", capabilities_json=["tool_use"],
        feature_metadata_json=PROFILE["feature_metadata_json"])
    provider = SimpleNamespace(id="provider-id", name="anthropic", kind="anthropic")
    row = ("candidate", model, provider)
    prepared = SimpleNamespace(application_policy=RequestConstraints(), request_constraints=RequestConstraints(),
        routing_snapshot=SimpleNamespace(candidates=(row,)))
    payload = SimpleNamespace(tools=[{"type": "function", "function": {"name": "score", "strict": True,
        "parameters": {"type": "object", "properties": {"score": {"type": "number", "maximum": 100}},
            "required": ["score"], "additionalProperties": False}}}], n=1, messages=[], response_format=None,
        **{key: None for key in ("temperature", "top_p", "stop", "seed", "frequency_penalty",
            "presence_penalty", "parallel_tool_calls", "tool_choice")})

    with pytest.raises(RuntimeAPIError) as caught:
        preflight_options(payload, prepared)

    assert caught.value.status_code == 400
    assert caught.value.details["param"] == "tools"


@pytest.mark.asyncio
async def test_provider_requires_profile_instead_of_loading_manifest_defaults():
    provider = AnthropicProvider(api_key="fixture", client=FakeClient())
    with pytest.raises(ProviderConfigurationError, match="database model profile"):
        await provider.generate("x", task_id="t1", model_id="unknown", timeout_seconds=3)
