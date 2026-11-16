import json
from types import SimpleNamespace

import pytest

from src.backend_ai.gateway.providers.anthropic_messages import (
    build_anthropic_request,
    normalize_message_response,
)
from src.backend_ai.gateway.providers.base import GenerationOptions, ProviderOutputError


def _profile(**constraints):
    return {
        "max_output_tokens": 128_000,
        "feature_metadata_json": {
            "constraints": {
                "thinking_mode": "adaptive",
                "default_effort": "medium",
                "forced_tool_choice": "conditional",
                **constraints,
            }
        },
    }


def test_request_keeps_system_multi_turn_tool_order_and_caps_thinking_tokens():
    options = GenerationOptions(
        max_output_tokens=4096,
        hard_completion_token_limit=2048,
        messages=(
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "What is 1+1?"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-a", "type": "function", "function": {
                    "name": "add", "arguments": '{"a":1,"b":1}'}}
            ]},
            {"role": "tool", "tool_call_id": "call-a", "content": "2"},
            {"role": "assistant", "content": "It is 2."},
            {"role": "user", "content": "And 2+2?"},
        ),
        tools=({"type": "function", "function": {"name": "add", "description": "Add numbers",
            "parameters": {"type": "object", "properties": {"a": {"type": "integer"},
                "b": {"type": "integer"}}, "required": ["a", "b"], "additionalProperties": False},
            "strict": True}},),
        parallel_tool_calls=False,
        low_reasoning=True,
    )

    request = build_anthropic_request("claude-haiku-5-5", "ignored", options, _profile())

    assert request["system"] == "Be concise."
    assert request["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "What is 1+1?"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call-a", "name": "add",
            "input": {"a": 1, "b": 1}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-a", "content": "2"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "It is 2."}]},
        {"role": "user", "content": [{"type": "text", "text": "And 2+2?"}]},
    ]
    assert request["tools"] == [{"name": "add", "description": "Add numbers",
        "input_schema": options.tools[0]["function"]["parameters"], "strict": True}]
    assert request["max_tokens"] == 2048
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "low"}
    assert request["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}


def test_request_supports_strict_json_schema_and_forced_tool_choice_only_for_conditional_profile():
    options = GenerationOptions(
        messages=({"role": "user", "content": "Return structured data"},),
        response_format={"type": "json_schema", "json_schema": {"name": "Answer", "strict": True,
            "schema": {"type": "object", "properties": {"answer": {"type": "string"}},
                "required": ["answer"], "additionalProperties": False}}},
        tools=({"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}},),
        tool_choice={"type": "function", "function": {"name": "lookup"}},
    )

    request = build_anthropic_request("claude-haiku-5-5", "ignored", options, _profile())

    assert request["output_config"]["format"] == {"type": "json_schema", "schema":
        options.response_format["json_schema"]["schema"]}
    assert request["tool_choice"] == {"type": "tool", "name": "lookup"}


@pytest.mark.parametrize("options, param", [
    (GenerationOptions(messages=({"role": "user", "content": "x"},), temperature=0), "temperature"),
    (GenerationOptions(messages=({"role": "user", "content": "x"},), top_p=0.5), "top_p"),
    (GenerationOptions(messages=({"role": "user", "content": "x"},), seed=1), "seed"),
    (GenerationOptions(messages=({"role": "user", "content": "x"},), n=2), "n"),
    (GenerationOptions(messages=({"role": "user", "content": [{"type": "image_url", "image_url": {
        "url": "https://example.test/image.png"}}]},)), "image_url"),
    (GenerationOptions(messages=({"role": "user", "content": "x"},), response_format={"type": "json_object"}),
     "response_format"),
])
def test_request_rejects_unsupported_params_instead_of_dropping_them(options, param):
    with pytest.raises(ValueError, match=param):
        build_anthropic_request("claude-haiku-5-5", "ignored", options, _profile())


@pytest.mark.parametrize("schema, keyword", [
    ({"type": "object", "properties": {"score": {"type": "number", "minimum": 0}},
      "required": ["score"], "additionalProperties": False}, "minimum"),
    ({"type": "object", "properties": {"name": {"type": "string", "maxLength": 32}},
      "required": ["name"], "additionalProperties": False}, "maxLength"),
    ({"type": "object", "properties": {}, "additionalProperties": True}, "additionalProperties"),
])
def test_request_rejects_json_schema_constraints_outside_anthropic_subset(schema, keyword):
    options = GenerationOptions(messages=({"role": "user", "content": "Return JSON"},),
        response_format={"type": "json_schema", "json_schema": {"name": "Answer", "schema": schema}})

    with pytest.raises(ValueError, match=keyword):
        build_anthropic_request("claude-haiku-5-5", "ignored", options, _profile())


def test_request_rejects_unsupported_constraints_in_strict_tool_schema():
    options = GenerationOptions(messages=({"role": "user", "content": "Call this tool"},), tools=(
        {"type": "function", "function": {"name": "score", "strict": True, "parameters": {
            "type": "object", "properties": {"value": {"type": "number", "maximum": 100}},
            "required": ["value"], "additionalProperties": False}}},))

    with pytest.raises(ValueError, match="maximum"):
        build_anthropic_request("claude-haiku-5-5", "ignored", options, _profile())


def test_request_uses_database_profile_and_rejects_forced_tools_when_profile_disallows_them():
    options = GenerationOptions(messages=({"role": "user", "content": "x"},),
        tools=({"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}},),
        tool_choice="required")

    with pytest.raises(ValueError, match="tool_choice"):
        build_anthropic_request("claude-opus-5-5", "ignored", options,
            _profile(forced_tool_choice="unsupported", default_effort="high", thinking_mode="always_on"))


def test_always_on_profile_uses_adaptive_thinking_and_keeps_truncation_when_tool_block_exists():
    request = build_anthropic_request("claude-opus-5-5", "hello", None,
        _profile(forced_tool_choice="unsupported", default_effort="medium", thinking_mode="always_on"))
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "medium"

    response = SimpleNamespace(content=[SimpleNamespace(type="tool_use", id="tool-1", name="run",
        input={"code": "x"})], stop_reason="max_tokens",
        usage=SimpleNamespace(input_tokens=4, output_tokens=128_000))
    normalized = normalize_message_response(response, model_id="claude-opus-5-5", task_id="t1")
    assert normalized.output_evidence.finish_reason == "length"
    assert normalized.metadata["openai_choices"][0]["finish_reason"] == "length"


def test_response_normalizes_tool_only_output_and_keeps_cache_usage_components():
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="private chain of thought", signature="sig-secret"),
            SimpleNamespace(type="tool_use", id="tool-2", name="lookup", input={"city": "Hanoi"}),
            SimpleNamespace(type="tool_use", id="tool-7", name="weather", input={"city": "Hue"}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=10, output_tokens=7, cache_read_input_tokens=3,
            cache_creation_input_tokens=5, cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=2,
                ephemeral_1h_input_tokens=3)),
    )

    normalized = normalize_message_response(response, model_id="claude-haiku-5-5", task_id="t1")

    assert normalized.text == ""
    assert normalized.input_tokens == 18
    assert normalized.output_tokens == 7
    assert normalized.metadata["anthropic_usage"] == {
        "input_tokens": 10, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 5,
        "cache_creation_5m_input_tokens": 2, "cache_creation_1h_input_tokens": 3,
    }
    assert normalized.output_evidence.cache_write_5m_tokens == 2
    assert normalized.output_evidence.cache_write_1h_tokens == 3
    assert normalized.output_evidence.finish_reason == "tool_calls"
    calls = normalized.metadata["openai_choices"][0]["message"]["tool_calls"]
    assert [call["id"] for call in calls] == ["tool-2", "tool-7"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Hanoi"}
    assert "private chain of thought" not in str(normalized.model_dump() if hasattr(normalized, "model_dump") else normalized)


def test_response_rejects_thinking_only_output_but_keeps_billable_receipt():
    response = SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="secret")],
        stop_reason="max_tokens", usage=SimpleNamespace(input_tokens=5, output_tokens=21))
    with pytest.raises(ProviderOutputError) as caught:
        normalize_message_response(response, model_id="claude-opus-5-5", task_id="probe")
    assert caught.value.code == "OUTPUT_TOKEN_LIMIT"
    assert caught.value.response.output_tokens == 21


def test_response_reconstructs_total_cached_input_from_reported_cache_ttl_components():
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=2, cache_read_input_tokens=4,
            cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=3, ephemeral_1h_input_tokens=5)))

    normalized = normalize_message_response(response, model_id="claude-sonnet-5-5", task_id="t1")

    assert normalized.input_tokens == 22
    assert normalized.metadata["anthropic_usage"]["cache_creation_input_tokens"] == 8
    assert normalized.output_evidence.cache_write_5m_tokens == 3
    assert normalized.output_evidence.cache_write_1h_tokens == 5
