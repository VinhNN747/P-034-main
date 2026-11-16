from types import SimpleNamespace
from typing import get_type_hints

import pytest
from openai.types.responses.response_create_params import ResponseCreateParamsNonStreaming
from pydantic import ValidationError

from src.backend_ai.dtos.openai_responses import ResponseCreateRequest
from src.backend_ai.gateway.model_alias import resolve_model
from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.backend_ai.services.openai_responses_validation import (
    RESPONSE_PARAMETER_SUPPORT,
    check_response_output,
    to_chat_request,
    to_runtime_input,
    validate_response_support,
)


def test_all_pinned_sdk_response_fields_are_classified():
    sdk_fields = set(get_type_hints(ResponseCreateParamsNonStreaming, include_extras=True))
    classified_roots = {path.split(".", 1)[0] for path in RESPONSE_PARAMETER_SUPPORT}
    assert sdk_fields <= classified_roots
    assert {
        "text.format.type", "text.format.strict", "text.verbosity", "stream_options.include_obfuscation",
        "input[*].type", "input[*].content[*].type", "input[*].content[*].annotations", "input[*].phase",
    } <= set(RESPONSE_PARAMETER_SUPPORT)


def test_text_and_manual_history_preserve_order_and_instructions():
    payload = ResponseCreateRequest.model_validate({
        "model": "auto",
        "input": [
            {"role": "user", "content": "xin chào"},
            {"id": "msg_prev", "type": "message", "role": "assistant", "status": "completed",
             "phase": "final_answer",
             "content": [{"type": "output_text", "text": "hello", "annotations": [], "logprobs": []}]},
            {"role": "user", "content": [{"type": "input_text", "text": "next"}]},
        ],
        "instructions": "be concise",
        "store": False,
    })
    validate_response_support(payload)
    runtime = to_runtime_input(payload)
    assert [message.role for message in runtime.messages] == ["developer", "user", "assistant", "user"]
    assert [message.text() for message in runtime.messages] == ["be concise", "xin chào", "hello", "next"]


def test_text_input_and_text_format_defaults_are_supported():
    payload = ResponseCreateRequest.model_validate({"model": "auto", "input": "  preserve  ", "store": False})
    validate_response_support(payload)
    assert to_runtime_input(payload).messages[0].content == "  preserve  "
    json_object = ResponseCreateRequest.model_validate({
        "model": "auto", "input": "{}", "store": False, "text": {"format": {"type": "json_object"}}
    })
    validate_response_support(json_object)
    assert to_runtime_input(json_object, resolve_model("gpt-test")).constraints.allowed_models == ["gpt-test"]


@pytest.mark.parametrize("output", ['{"ok":true}', '{"x":NaN}', '[1,2]', 'not json'])
def test_json_object_response_output_is_checked_before_finalize(output):
    payload = ResponseCreateRequest.model_validate({
        "model": "auto", "input": "x", "store": False,
        "text": {"format": {"type": "json_object"}},
    })
    result = SimpleNamespace(
        aggregation=SimpleNamespace(status=SimpleNamespace(value="complete"), final_answer=output),
        output_evidence=__import__("src.shared.dtos.gateway", fromlist=["OutputEvidence"]).OutputEvidence(finish_reason="stop"),
        openai_choices=None
    )
    policy = check_response_output(result, payload)
    assert (policy.error is None) is (output == '{"ok":true}')
    if policy.error is not None:
        assert policy.error.details["param"] == "text.format"


def test_partial_response_is_rejected_by_output_policy():
    payload = ResponseCreateRequest.model_validate({"model": "auto", "input": "x", "store": False})
    result = SimpleNamespace(aggregation=SimpleNamespace(status=SimpleNamespace(value="partial")))
    assert check_response_output(result, payload).error.code == "PARTIAL_RESPONSE_NOT_SUPPORTED"


@pytest.mark.parametrize("body", [
    {"model": "auto", "input": "x", "store": False, "made_up": 1},
    {"model": "auto", "input": "x", "store": False, "text": {"format": {"type": "json_object", "oops": 1}}},
    {"model": "auto", "input": [{"role": "user", "content": [{"type": "input_text", "text": "x", "oops": 1}]}], "store": False},
    {"model": "auto", "input": [{"role": "user", "content": [{"type": "input_image", "image_url": "data"}]}], "store": False},
])
def test_unknown_or_non_text_nested_fields_are_not_ignored(body):
    with pytest.raises(ValidationError):
        ResponseCreateRequest.model_validate(body)


def test_assistant_history_rejects_unsupported_phase():
    with pytest.raises(ValidationError):
        ResponseCreateRequest.model_validate({
            "model": "auto", "store": False,
            "input": [{"id": "msg_prev", "type": "message", "role": "assistant", "status": "completed",
                       "phase": "commentary", "content": [{"type": "output_text", "text": "hello"}]}],
        })


@pytest.mark.parametrize("store", [None, True])
def test_store_defaults_true_and_accepts_explicit_true(store):
    body = {"model": "auto", "input": "x"}
    if store is not None:
        body["store"] = store
    payload = ResponseCreateRequest.model_validate(body)
    validate_response_support(payload)
    assert payload.store is True


def test_supported_noops_and_unsupported_features_are_explicit():
    noops = ResponseCreateRequest.model_validate({
        "model": "auto", "input": "x", "store": False, "background": False,
        "tools": [], "tool_choice": "none", "parallel_tool_calls": False,
        "truncation": "disabled", "stream_options": {},
    })
    validate_response_support(noops)
    unsupported = ResponseCreateRequest.model_validate({
        "model": "auto", "input": "x", "store": False, "background": True,
    })
    with pytest.raises(OpenAIAPIError) as error:
        validate_response_support(unsupported)
    assert error.value.param == "background"

    stream_options = ResponseCreateRequest.model_validate({
        "model": "auto", "input": "x", "store": False,
        "stream_options": {"include_obfuscation": False},
    })
    with pytest.raises(OpenAIAPIError) as error:
        validate_response_support(stream_options)
    assert error.value.param == "stream_options.include_obfuscation"


@pytest.mark.parametrize("body,param", [
    ({"max_output_tokens": 1}, "max_output_tokens"),
    ({"max_output_tokens": 32768}, "max_output_tokens"),
    ({"temperature": 0}, "temperature"),
    ({"top_p": 0.5}, "top_p"),
    ({"text": {"format": {"type": "json_schema", "name": "x", "schema": {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False}, "strict": True}}}, "text.format"),
    ({"tools": [{"type": "function", "name": "lookup"}]}, "tools"),
    ({"previous_response_id": "resp_old"}, "previous_response_id"),
])
def test_supported_response_options_are_preserved(body, param):
    payload = ResponseCreateRequest.model_validate({"model": "auto", "input": "x", "store": False, **body})
    validate_response_support(payload)
    chat = to_chat_request(payload, to_runtime_input(payload).messages)
    if param == "max_output_tokens":
        assert chat.max_completion_tokens == body[param]
    elif param in {"temperature", "top_p"}:
        assert getattr(chat, param) == body[param]
    elif param == "text.format":
        assert chat.response_format["json_schema"]["schema"] == body["text"]["format"]["schema"]
    elif param == "tools":
        assert chat.tools[0]["function"]["name"] == "lookup"
    else:
        assert payload.previous_response_id == "resp_old"


@pytest.mark.parametrize("body", [
    {"max_output_tokens": True},
    {"max_output_tokens": -1},
    {"max_output_tokens": 32769},
    {"store": None},
    {"stream": 1},
    {"text": None},
])
def test_invalid_strict_types_ranges_and_nulls_fail_validation(body):
    with pytest.raises(ValidationError):
        ResponseCreateRequest.model_validate({"model": "auto", "input": "x", "store": False, **body})


def test_nonempty_annotations_and_invalid_assistant_history_are_rejected():
    with pytest.raises(ValidationError):
        ResponseCreateRequest.model_validate({
            "model": "auto", "store": False,
            "input": [{"id": "msg", "type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": "x", "annotations": [{"type": "url_citation"}], "logprobs": []}]}],
        })
    with pytest.raises(ValidationError):
        ResponseCreateRequest.model_validate({"model": "auto", "store": False,
                                              "input": [{"role": "assistant", "content": "x"}]})


def test_message_count_and_metadata_limits():
    payload = ResponseCreateRequest.model_validate({
        "model": "auto", "store": False, "metadata": {"key": "v" * 512},
        "input": [{"role": "user", "content": "x"}] * 200,
    })
    validate_response_support(payload)
    with pytest.raises(OpenAIAPIError) as error:
        to_runtime_input(payload.model_copy(update={"instructions": "developer"}))
    assert error.value.param == "input"


def test_metadata_limit_matches_wrapped_runtime_metadata_size():
    payload = ResponseCreateRequest.model_validate({
        "model": "auto", "input": "x", "store": False,
        "metadata": {f"key-{index}": "v" * 512 for index in range(16)},
    })
    with pytest.raises(OpenAIAPIError) as error:
        to_runtime_input(payload)
    assert error.value.status_code == 400
    assert error.value.param == "metadata"


def test_response_text_input_limit_is_bytes_not_200_characters():
    payload = ResponseCreateRequest(model='auto', input='x' * 201)
    validate_response_support(payload)
    assert to_runtime_input(payload).messages[0].content == 'x' * 201


def test_response_list_input_keeps_200_message_limit():
    with pytest.raises(ValidationError):
        ResponseCreateRequest(model='auto', input=[{'role':'user','content':'x'}] * 201)
