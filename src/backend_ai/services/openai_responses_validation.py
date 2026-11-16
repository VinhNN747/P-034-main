from __future__ import annotations

import json
from typing import Literal

from src.backend_ai.dtos.openai_responses import (
    ResponseCreateRequest,
    ResponseFunctionCall,
    ResponseFunctionCallOutput,
    ResponseInputMessage,
)
from src.backend_ai.gateway.model_alias import ModelSelection
from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.backend_ai.services.runtime_request_limits import MAX_RUNTIME_METADATA_BYTES
from src.shared.dtos.gateway import ChatMessage, PipelineResult, TextContentPart
from src.shared.dtos.runtime_api import (
    RequestConstraints,
    RuntimeOperationError,
    RuntimeOutputPolicy,
    RuntimeRequestInput,
)

ResponseSupport = Literal["supported", "unsupported", "noop"]

RESPONSE_PARAMETER_SUPPORT: dict[str, ResponseSupport] = {
    "model": "supported",
    "input": "supported",
    "instructions": "supported",
    "store": "supported",
    "stream": "supported",
    "text.format.type": "supported",
    "text.format.name": "supported",
    "text.format.schema": "supported",
    "text.format.description": "supported",
    "text.format.strict": "supported",
    "text.verbosity": "unsupported",
    "metadata": "supported",
    "background": "noop",
    "context_management": "unsupported",
    "conversation": "unsupported",
    "include": "unsupported",
    "max_output_tokens": "supported",
    "max_tool_calls": "unsupported",
    "moderation": "unsupported",
    "parallel_tool_calls": "supported",
    "previous_response_id": "supported",
    "prompt": "unsupported",
    "prompt_cache_key": "unsupported",
    "prompt_cache_options": "unsupported",
    "prompt_cache_retention": "unsupported",
    "reasoning": "unsupported",
    "safety_identifier": "unsupported",
    "service_tier": "unsupported",
    "stream_options": "noop",
    "stream_options.include_obfuscation": "unsupported",
    "temperature": "supported",
    "tool_choice": "supported",
    "tools": "supported",
    "top_logprobs": "unsupported",
    "top_p": "supported",
    "truncation": "noop",
    "user": "unsupported",
    "input[*].type": "supported",
    "input[*].role": "supported",
    "input[*].content": "supported",
    "input[*].content[*].type": "supported",
    "input[*].content[*].text": "supported",
    "input[*].content[*].annotations": "noop",
    "input[*].content[*].logprobs": "noop",
    "input[*].id": "supported",
    "input[*].status": "supported",
    "input[*].phase": "supported",
}


def validate_response_support(payload: ResponseCreateRequest) -> None:
    if payload.background not in (None, False):
        raise _unsupported("background", "Background execution is not supported.")
    if payload.truncation not in (None, "disabled"):
        raise _unsupported("truncation", "Automatic truncation is not supported.")
    if payload.stream_options is not None:
        if payload.stream_options.model_fields_set:
            field = next(iter(payload.stream_options.model_fields_set))
            raise _unsupported(f"stream_options.{field}", "Responses stream options are not supported.")
    for name in (
        "context_management", "conversation", "include", "max_tool_calls", "moderation",
        "prompt", "prompt_cache_key", "prompt_cache_options", "prompt_cache_retention",
        "reasoning", "safety_identifier", "service_tier", "top_logprobs", "user",
    ):
        if getattr(payload, name) is not None:
            raise _unsupported(name, f"The Responses parameter '{name}' is not supported.")
    format_value = payload.text.format
    if format_value is not None:
        if format_value.type not in {"text", "json_object", "json_schema"}:
            raise _unsupported("text.format.type", "This text format is not supported.")
    if payload.text.verbosity is not None:
        raise _unsupported("text.verbosity", "Text verbosity is not supported.")
    if isinstance(payload.input, list):
        message_count = len(payload.input) + int(payload.instructions is not None)
        if message_count > 200:
            raise OpenAIAPIError(400, "Responses input supports at most 200 messages including instructions.",
                                 code="invalid_request", param="input")
    from src.backend_ai.services.openai_chat_validation import validate_chat_support
    try:
        validate_chat_support(to_chat_request(payload, [ChatMessage(role="user", content="validation")]))
    except OpenAIAPIError as exc:
        if exc.param == "response_format":
            exc.param = "text.format"
        raise


def to_runtime_input(
    payload: ResponseCreateRequest, selection: ModelSelection | None = None
) -> RuntimeRequestInput:
    messages: list[ChatMessage] = []
    if payload.instructions is not None:
        messages.append(ChatMessage(role="developer", content=payload.instructions))
    if isinstance(payload.input, str):
        messages.append(ChatMessage(role="user", content=payload.input))
    else:
        for item in payload.input:
            if isinstance(item, ResponseFunctionCall):
                call = {"id": item.call_id, "type": "function", "function": {"name": item.name, "arguments": item.arguments}}
                if messages and messages[-1].role == "assistant" and messages[-1].tool_calls:
                    from src.shared.dtos.gateway import ToolCall
                    messages[-1].tool_calls.append(ToolCall.model_validate(call))
                else:
                    messages.append(ChatMessage(role="assistant", content=None, tool_calls=[call]))
            elif isinstance(item, ResponseFunctionCallOutput):
                messages.append(ChatMessage(role="tool", content=item.output, tool_call_id=item.call_id))
            else:
                messages.append(_to_runtime_message(item))
    if len(messages) > 200:
        raise OpenAIAPIError(400, "Responses input supports at most 200 messages including instructions.",
                             code="invalid_request", param="input")
    constraint_values = {"max_output_tokens": payload.max_output_tokens}
    if selection is not None:
        constraint_values.update(
            {
                "quality_priority": selection.quality_priority,
                "allowed_models": [selection.pinned_model] if selection.pinned_model else None,
            }
        )
    runtime_metadata = {
        "api_surface": "openai_responses",
        "model_alias": payload.model,
        "client_metadata": payload.metadata or {},
        "openai_hard_completion_token_limit": payload.max_output_tokens,
    }
    metadata_size = len(json.dumps(
        runtime_metadata,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8"))
    if metadata_size > MAX_RUNTIME_METADATA_BYTES:
        raise OpenAIAPIError(
            400,
            f"metadata exceeds the runtime limit of {MAX_RUNTIME_METADATA_BYTES} bytes.",
            code="invalid_request",
            param="metadata",
        )
    return RuntimeRequestInput(
        messages=messages,
        constraints=RequestConstraints.model_validate(
            {key: value for key, value in constraint_values.items() if value is not None}
        ),
        metadata=runtime_metadata,
    )


def to_chat_request(payload, messages):
    from src.backend_ai.dtos.openai_compat import ChatCompletionRequest
    tools = []
    for tool in payload.tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise _unsupported("tools", "Only function tools are supported.")
        function = {key: value for key, value in tool.items() if key != "type"}
        tools.append({"type": "function", "function": function})
    choice = payload.tool_choice
    if isinstance(choice, dict):
        choice = {"type": choice.get("type"), "function": {"name": choice.get("name")}}
    format_value = payload.text.format
    response_format = None
    if format_value is not None:
        dump = format_value.model_dump(mode="json", by_alias=True, exclude_none=True)
        response_format = {"type": dump.pop("type"), "json_schema": dump} if format_value.type == "json_schema" else dump
    return ChatCompletionRequest(model=payload.model, messages=messages, tools=tools,
        tool_choice=choice, parallel_tool_calls=payload.parallel_tool_calls,
        temperature=payload.temperature, top_p=payload.top_p, max_completion_tokens=payload.max_output_tokens,
        response_format=response_format)


def check_response_output(result: PipelineResult, payload: ResponseCreateRequest) -> RuntimeOutputPolicy:
    if result.aggregation is None:
        return RuntimeOutputPolicy()
    if result.aggregation.status.value == "partial":
        return RuntimeOutputPolicy(error=RuntimeOperationError(code="PARTIAL_RESPONSE_NOT_SUPPORTED",
            message="Partial Responses output is not supported by this gateway profile.", details={}))
    from src.backend_ai.routers.openai_compat import _check_chat_output
    policy = _check_chat_output(result, to_chat_request(payload, [ChatMessage(role="user", content="validation")]))
    if policy.error and policy.error.details.get("param") == "response_format":
        policy.error.details["param"] = "text.format"
    return policy


def _to_runtime_message(message: ResponseInputMessage) -> ChatMessage:
    if isinstance(message.content, str):
        content: str | list[TextContentPart] = message.content
    else:
        content = [TextContentPart(text=part.text) for part in message.content]
    return ChatMessage(role=message.role, content=content)


def _unsupported(param: str, message: str) -> OpenAIAPIError:
    return OpenAIAPIError(400, message, code="unsupported_parameter", param=param)


def _reject_non_finite(value: str):
    raise ValueError(f"Invalid JSON numeric constant: {value}")
