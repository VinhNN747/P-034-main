"""OpenAI Chat to Anthropic Messages request/response mapping.

This module deliberately accepts only the gateway's text and custom function
tool profile. Provider-specific content blocks such as images, PDFs, citations,
and server tools stay out of the request until the gateway implements them.
"""

from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any

from src.backend_ai.gateway.providers.base import (
    GenerationOptions,
    ProviderOutputError,
    ProviderResponse,
)
from src.shared.dtos.gateway import OutputEvidence

_ANTHROPIC_SCHEMA_KEYWORDS = {
    "$def", "$defs", "$ref", "additionalProperties", "allOf", "anyOf", "const", "default",
    "definitions", "description", "enum", "format", "items", "minItems", "pattern", "properties",
    "required", "title", "type",
}
_ANTHROPIC_SCHEMA_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}
_ANTHROPIC_SCHEMA_FORMATS = {
    "date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid",
}


def validate_anthropic_json_schema(schema: Any) -> None:
    """Reject JSON Schema keywords outside the Messages structured-output subset."""
    if not isinstance(schema, dict):
        raise ValueError("Anthropic JSON schema must be an object")
    _validate_schema_node(schema, schema, path="$", ref_stack=frozenset())


def _validate_schema_node(node: Any, root: dict[str, Any], *, path: str, ref_stack: frozenset[str]) -> None:
    if not isinstance(node, dict):
        raise ValueError(f"Anthropic JSON schema at {path} must be an object")
    unsupported = set(node) - _ANTHROPIC_SCHEMA_KEYWORDS
    if unsupported:
        keyword = sorted(unsupported)[0]
        raise ValueError(f"Anthropic JSON schema keyword {keyword!r} is unsupported")

    schema_type = node.get("type")
    types = schema_type if isinstance(schema_type, list) else [schema_type] if schema_type is not None else []
    if any(not isinstance(item, str) or item not in _ANTHROPIC_SCHEMA_TYPES for item in types):
        raise ValueError(f"Anthropic JSON schema at {path} has an unsupported type")
    if isinstance(schema_type, list) and not schema_type:
        raise ValueError(f"Anthropic JSON schema at {path} has an empty type union")

    if "object" in types or "properties" in node:
        if node.get("additionalProperties") is not False:
            raise ValueError(f"Anthropic JSON schema at {path} requires additionalProperties=false")
    if "additionalProperties" in node and node["additionalProperties"] is not False:
        raise ValueError(f"Anthropic JSON schema at {path} supports additionalProperties=false only")
    required = node.get("required")
    if required is not None and (not isinstance(required, list) or any(not isinstance(item, str) for item in required)):
        raise ValueError(f"Anthropic JSON schema at {path} has an invalid required list")
    enum = node.get("enum")
    if enum is not None and (not isinstance(enum, list) or any(isinstance(item, (dict, list)) for item in enum)):
        raise ValueError(f"Anthropic JSON schema at {path} supports primitive enum values only")
    if isinstance(node.get("const"), (dict, list)):
        raise ValueError(f"Anthropic JSON schema at {path} supports primitive const values only")
    schema_format = node.get("format")
    if schema_format is not None and schema_format not in _ANTHROPIC_SCHEMA_FORMATS:
        raise ValueError(f"Anthropic JSON schema format {schema_format!r} is unsupported")
    min_items = node.get("minItems")
    if min_items is not None and (type(min_items) is not int or min_items not in {0, 1}):
        raise ValueError("Anthropic JSON schema minItems only supports 0 or 1")
    pattern = node.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str) or re.search(r"\\[1-9]|\\[bB]|\(\?[=!<]|\(\?P", pattern):
            raise ValueError(f"Anthropic JSON schema pattern at {path} uses unsupported regex features")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Anthropic JSON schema pattern at {path} is invalid") from exc

    if "allOf" in node and _contains_ref(node["allOf"]):
        raise ValueError(f"Anthropic JSON schema allOf at {path} does not support $ref")
    reference = node.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#"):
            raise ValueError(f"Anthropic JSON schema at {path} does not support external $ref")
        if reference in ref_stack:
            raise ValueError(f"Anthropic JSON schema at {path} does not support recursive schemas")
        target = _resolve_local_schema_ref(root, reference)
        _validate_schema_node(target, root, path=reference, ref_stack=ref_stack | {reference})

    for keyword in ("properties", "$def", "$defs", "definitions"):
        children = node.get(keyword)
        if children is None:
            continue
        if not isinstance(children, dict):
            raise ValueError(f"Anthropic JSON schema {keyword} at {path} must be an object")
        for name, child in children.items():
            _validate_schema_node(child, root, path=f"{path}.{keyword}.{name}", ref_stack=ref_stack)
    for keyword in ("items", "anyOf", "allOf"):
        children = node.get(keyword)
        if children is None:
            continue
        if keyword == "items" and isinstance(children, dict):
            _validate_schema_node(children, root, path=f"{path}.items", ref_stack=ref_stack)
        elif isinstance(children, list) and children:
            for index, child in enumerate(children):
                _validate_schema_node(child, root, path=f"{path}.{keyword}[{index}]", ref_stack=ref_stack)
        else:
            raise ValueError(f"Anthropic JSON schema {keyword} at {path} must contain schema objects")


def _contains_ref(value: Any) -> bool:
    if isinstance(value, dict):
        return "$ref" in value or any(_contains_ref(child) for child in value.values())
    return isinstance(value, list) and any(_contains_ref(child) for child in value)


def _resolve_local_schema_ref(root: dict[str, Any], reference: str) -> Any:
    if reference == "#":
        return root
    if not reference.startswith("#/"):
        raise ValueError(f"Anthropic JSON schema reference {reference!r} is invalid")
    value: Any = root
    for segment in reference[2:].split("/"):
        key = segment.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"Anthropic JSON schema reference {reference!r} cannot be resolved")
        value = value[key]
    return value


def build_anthropic_request(model_id: str, prompt: str, options: GenerationOptions | None,
                            profile: Any) -> dict[str, Any]:
    """Build a native Messages API request using only the persisted model profile."""
    options = options or GenerationOptions()
    constraints = _constraints(profile)
    if not constraints:
        raise ValueError("database model profile has no Anthropic request constraints")

    for name in ("temperature", "top_p", "seed", "frequency_penalty", "presence_penalty"):
        if getattr(options, name) is not None:
            raise ValueError(f"Anthropic profile does not support {name}")
    if options.n != 1:
        raise ValueError("Anthropic profile does not support n other than 1")
    if options.response_format and options.response_format.get("type") != "json_schema":
        raise ValueError("Anthropic profile supports response_format json_schema only")
    if options.response_format and options.json_schema:
        raise ValueError("Anthropic profile received two JSON schemas")

    request: dict[str, Any] = {
        "model": model_id,
        "max_tokens": _max_tokens(options, profile),
        "messages": [],
    }
    messages = options.messages
    if messages is None:
        messages = ({"role": "user", "content": prompt},)

    system: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role in {"system", "developer"}:
            text = _text_content(message.get("content"), "system")
            if text:
                system.append(text)
            continue
        if message.get("refusal"):
            raise ValueError("Anthropic profile does not accept refusal fields in message history")
        if role == "tool":
            tool_id = message.get("tool_call_id")
            if not isinstance(tool_id, str) or not tool_id:
                raise ValueError("Anthropic tool_result requires tool_call_id")
            blocks = [{"type": "tool_result", "tool_use_id": tool_id,
                       "content": _text_content(message.get("content"), "tool result")}]
            _append_message(converted, "user", blocks)
            continue
        if role not in {"user", "assistant"}:
            raise ValueError(f"Anthropic profile does not support message role {role!r}")

        blocks = _content_blocks(message.get("content"), role)
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ValueError("Anthropic tool call arguments must be valid JSON") from exc
                if not isinstance(arguments, dict):
                    raise ValueError("Anthropic tool call arguments must be a JSON object")
                call_id = call.get("id")
                name = function.get("name")
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                    raise ValueError("Anthropic tool calls require an id and function name")
                blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": arguments})
        if not blocks:
            raise ValueError(f"Anthropic {role} message has no supported content")
        _append_message(converted, role, blocks)

    if not converted:
        raise ValueError("Anthropic Messages API requires at least one user or assistant message")
    request["messages"] = converted
    if system:
        request["system"] = "\n\n".join(system)

    if options.stop is not None:
        stop = [options.stop] if isinstance(options.stop, str) else list(options.stop)
        if not stop or any(not isinstance(item, str) or not item for item in stop):
            raise ValueError("Anthropic stop sequences must be non-empty strings")
        request["stop_sequences"] = stop

    if options.tools:
        request["tools"] = _tools(options.tools)
    if options.tool_choice is not None:
        if not options.tools:
            raise ValueError("Anthropic tool_choice requires tools")
        request["tool_choice"] = _tool_choice(options.tool_choice, constraints)
    elif options.tools and options.parallel_tool_calls is False:
        request["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}

    if options.parallel_tool_calls is False and request.get("tool_choice", {}).get("type") in {"auto", "any", "tool"}:
        request["tool_choice"]["disable_parallel_tool_use"] = True
    elif options.parallel_tool_calls not in (None, True, False):
        raise ValueError("parallel_tool_calls must be a boolean")

    schema = options.json_schema
    if options.response_format:
        fmt = options.response_format.get("json_schema") or {}
        schema = fmt.get("schema")
        if not isinstance(schema, dict):
            raise ValueError("Anthropic json_schema response_format requires a schema object")
        validate_anthropic_json_schema(schema)
        if fmt.get("strict") is False:
            raise ValueError("Anthropic structured output requires strict schema mode")
    if schema is not None:
        if not isinstance(schema, dict):
            raise ValueError("Anthropic JSON schema must be an object")
        validate_anthropic_json_schema(schema)
        request["output_config"] = {"format": {"type": "json_schema", "schema": schema}}

    thinking_mode = constraints.get("thinking_mode")
    if thinking_mode not in {"adaptive", "always_on", "adaptive_legacy"}:
        raise ValueError("Anthropic profile has an unsupported thinking mode")
    # These five catalog profiles use adaptive thinking upstream, including the
    # two models where adaptive thinking is always enabled and the legacy 4.6 profile.
    request["thinking"] = {"type": "adaptive"}
    effort = "low" if options.low_reasoning else constraints.get("default_effort")
    if effort not in {"low", "medium", "high"}:
        raise ValueError("Anthropic profile has an invalid default effort")
    request.setdefault("output_config", {})["effort"] = effort
    return request


def normalize_message_response(response: Any, *, model_id: str, task_id: str,
                               started: float | None = None, allow_empty: bool = False) -> ProviderResponse:
    """Normalize a completed Messages API object, keeping thinking private."""
    start = started if started is not None else perf_counter()
    blocks = _get(response, "content", []) or []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        kind = _get(block, "type")
        if kind == "text":
            value = _get(block, "text")
            if isinstance(value, str):
                text_parts.append(value)
        elif kind == "tool_use":
            call_id = _get(block, "id")
            name = _get(block, "name")
            arguments = _get(block, "input", {})
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                continue
            tool_calls.append({"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))}})

    raw_usage = _get(response, "usage", {}) or {}
    usage = _usage_details(raw_usage)
    input_tokens = sum(usage[key] or 0 for key in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    output_tokens = _valid_int(_get(raw_usage, "output_tokens")) or 0
    finish_reason = _finish_reason(_get(response, "stop_reason"), bool(tool_calls))
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    choice_finish = ("length" if finish_reason == "length" else
                     "content_filter" if finish_reason == "content_filter" else
                     "tool_calls" if tool_calls else finish_reason)
    result = ProviderResponse(
        text="".join(text_parts), provider="anthropic", model_id=model_id,
        input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=max(0, perf_counter() - start) * 1000,
        usage_received=_valid_int(_get(raw_usage, "input_tokens")) is not None
            and _valid_int(_get(raw_usage, "output_tokens")) is not None,
        output_evidence=OutputEvidence(
            finish_reason=choice_finish,
            cached_tokens=usage["cache_read_input_tokens"],
            cache_write_tokens=usage["cache_creation_input_tokens"],
            cache_write_5m_tokens=usage["cache_creation_5m_input_tokens"],
            cache_write_1h_tokens=usage["cache_creation_1h_input_tokens"],
            reasoning_tokens=None,
        ),
        metadata={
            "task_id": task_id,
            "mode": "live",
            "finish_reason": choice_finish,
            "anthropic_usage": usage,
            "openai_choices": [{"index": 0, "message": message, "finish_reason": choice_finish}],
        },
    )
    if not allow_empty and not result.text and not tool_calls:
        code = "OUTPUT_TOKEN_LIMIT" if finish_reason == "length" else (
            "OUTPUT_BLOCKED" if finish_reason == "content_filter" else "OUTPUT_EMPTY")
        message_text = "The provider exhausted its output token limit before returning visible output." \
            if code == "OUTPUT_TOKEN_LIMIT" else "The provider returned no visible output or tool call."
        raise ProviderOutputError(code, message_text, result)
    return result


def stream_receipt(*, text_parts: list[str], tools: dict[int, dict[str, Any]], input_usage: dict[str, int | None],
                   output_tokens: int | None, stop_reason: str | None, model_id: str, task_id: str,
                   started: float, usage_received: bool, allow_empty: bool = False) -> ProviderResponse:
    raw = {"input_tokens": input_usage["input_tokens"],
           "cache_read_input_tokens": input_usage["cache_read_input_tokens"],
           "cache_creation_input_tokens": input_usage["cache_creation_input_tokens"],
           "cache_creation": {
               "ephemeral_5m_input_tokens": input_usage["cache_creation_5m_input_tokens"],
               "ephemeral_1h_input_tokens": input_usage["cache_creation_1h_input_tokens"],
           },
           "output_tokens": output_tokens}
    content = [type("Block", (), {"type": "text", "text": text})() for text in text_parts]
    content.extend(type("Tool", (), {"type": "tool_use", "id": tool["id"], "name": tool["name"],
        "input": tool["input"]})() for _, tool in sorted(tools.items()))
    response = type("AnthropicMessage", (), {"content": content, "usage": raw, "stop_reason": stop_reason})()
    result = normalize_message_response(response, model_id=model_id, task_id=task_id, started=started,
        allow_empty=allow_empty)
    if not usage_received:
        return result.__class__(**{**result.__dict__, "usage_received": False})
    return result


def _constraints(profile: Any) -> dict[str, Any]:
    feature_metadata = _get(profile, "feature_metadata_json") or _get(profile, "feature_metadata") or {}
    constraints = feature_metadata.get("constraints") if isinstance(feature_metadata, dict) else None
    if isinstance(constraints, dict):
        return constraints
    if isinstance(profile, dict) and "constraints" in profile:
        return profile["constraints"]
    return {}


def _max_tokens(options: GenerationOptions, profile: Any) -> int:
    profile_cap = _valid_int(_get(profile, "max_output_tokens"))
    requested = _valid_int(options.max_output_tokens)
    hard = _valid_int(options.hard_completion_token_limit)
    values = [value for value in (profile_cap, requested, hard) if value is not None]
    if not values:
        raise ValueError("Anthropic database model profile is missing max_output_tokens")
    cap = min(values)
    if cap < 1:
        raise ValueError("Anthropic max_tokens must be positive")
    return cap


def _content_blocks(content: Any, role: str) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise ValueError(f"Anthropic {role} content must be text")
    blocks = []
    for part in content:
        kind = _get(part, "type")
        if kind != "text":
            raise ValueError(f"Anthropic profile does not support {kind or 'unknown content'} input")
        text = _get(part, "text")
        if not isinstance(text, str):
            raise ValueError("Anthropic text content block requires text")
        blocks.append({"type": "text", "text": text})
    return blocks


def _text_content(content: Any, label: str) -> str:
    return "\n".join(block["text"] for block in _content_blocks(content, label))


def _append_message(messages: list[dict[str, Any]], role: str, content: list[dict[str, Any]]) -> None:
    # Messages API permits adjacent turns from the same role only after merging.
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(content)
    else:
        messages.append({"role": role, "content": content})


def _tools(tools: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    output = []
    names: set[str] = set()
    for tool in tools:
        if tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise ValueError("Anthropic profile supports custom function tools only")
        function = tool["function"]
        name = function.get("name")
        schema = function.get("parameters", function.get("input_schema"))
        if not isinstance(name, str) or not name or name in names or not isinstance(schema, dict):
            raise ValueError("Anthropic tools require unique names and an input schema")
        if function.get("strict") is True:
            validate_anthropic_json_schema(schema)
        names.add(name)
        mapped = {"name": name, "description": function.get("description") or "", "input_schema": schema}
        if function.get("strict") is True:
            mapped["strict"] = True
        output.append(mapped)
    return output


def _tool_choice(value: Any, constraints: dict[str, Any]) -> dict[str, Any]:
    if value == "auto":
        return {"type": "auto"}
    if value == "required":
        mode = "any"
    elif isinstance(value, dict) and value.get("type") == "function":
        mode = "tool"
        name = (value.get("function") or {}).get("name")
    elif value == "none":
        return {"type": "none"}
    else:
        raise ValueError("Anthropic profile does not support this tool_choice")
    if constraints.get("forced_tool_choice") not in {"conditional", "supported"}:
        raise ValueError("Anthropic model profile does not support forced tool_choice")
    result = {"type": mode}
    if mode == "tool":
        if not isinstance(name, str) or not name:
            raise ValueError("Anthropic named tool_choice requires a function name")
        result["name"] = name
    return result


def _usage_details(usage: Any) -> dict[str, int | None]:
    cache_creation = _get(usage, "cache_creation") or {}
    cache_5m = _valid_int(_get(cache_creation, "ephemeral_5m_input_tokens"))
    cache_1h = _valid_int(_get(cache_creation, "ephemeral_1h_input_tokens"))
    cache_creation_total = _valid_int(_get(usage, "cache_creation_input_tokens"))
    if cache_creation_total is None and (cache_5m is not None or cache_1h is not None):
        cache_creation_total = (cache_5m or 0) + (cache_1h or 0)
    return {
        "input_tokens": _valid_int(_get(usage, "input_tokens")),
        "cache_read_input_tokens": _valid_int(_get(usage, "cache_read_input_tokens")),
        "cache_creation_input_tokens": cache_creation_total,
        "cache_creation_5m_input_tokens": cache_5m,
        "cache_creation_1h_input_tokens": cache_1h,
    }


def _finish_reason(value: Any, has_tools: bool) -> str:
    if value == "max_tokens":
        return "length"
    if value == "refusal":
        return "content_filter"
    if value == "tool_use" or has_tools and value in {"end_turn", "stop_sequence", None}:
        return "tool_calls"
    return {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}.get(value, "unknown")


def _valid_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)
