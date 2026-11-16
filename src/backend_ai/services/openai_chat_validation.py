import re

from src.backend_ai.dtos.openai_compat import ChatCompletionRequest
from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.backend_ai.services.openai_compatibility_profile import STRICT_CHAT_PROFILE, OpenAICompatibilityProfile


def validate_chat_support(payload: ChatCompletionRequest, profile: OpenAICompatibilityProfile = STRICT_CHAT_PROFILE):
    for name in payload.model_fields_set:
        support = profile.parameter_support.get(name)
        if support is None and profile.unknown_parameter_policy == "reject":
            raise OpenAIAPIError(400, f"Unknown parameter '{name}'.", param=name, code="unknown_parameter")
        if support == "unsupported" and (
            getattr(payload, name, None) is not None or name in (payload.model_extra or {})
        ):
            raise OpenAIAPIError(400, f"Parameter '{name}' is not supported.", param=name, code="unsupported_parameter")
    if payload.max_tokens is not None and payload.max_completion_tokens is not None:
        raise OpenAIAPIError(
            400, "Specify only one completion token limit.", param="max_tokens", code="invalid_request"
        )
    if not payload.tools and payload.tool_choice not in (None, "none", "auto"):
        raise OpenAIAPIError(
            400, "tool_choice requires a non-empty tools list.", param="tool_choice", code="invalid_request"
        )
    if payload.tools:
        names = []
        for tool in payload.tools:
            function = tool.get("function", {})
            if tool.get("type") != "function":
                raise OpenAIAPIError(400, "Only function tools are supported.", param="tools", code="unsupported_parameter")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
                raise OpenAIAPIError(400, "Function tools require a name.", param="tools", code="invalid_request")
            if (set(tool) - {"type", "function"}
                or set(function) - {"name", "description", "parameters", "strict"}
                or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", function["name"]) is None
                or function.get("strict") is not None and type(function["strict"]) is not bool
                or function.get("description") is not None and not isinstance(function["description"], str)):
                raise OpenAIAPIError(400, "Invalid function metadata.", param="tools", code="invalid_request")
            from src.backend_ai.services.openai_schema_service import validate_schema_request
            if "parameters" in function:
                validate_schema_request(function["parameters"], param="tools", strict=function.get("strict") is True)
            names.append(function["name"])
        if len(set(names)) != len(names):
            raise OpenAIAPIError(400, "Function names must be unique.", param="tools", code="invalid_request")
        if isinstance(payload.tool_choice, dict):
            function = payload.tool_choice.get("function")
            named = function.get("name") if isinstance(function, dict) else None
            if payload.tool_choice.get("type") != "function" or named not in names:
                raise OpenAIAPIError(400, "tool_choice must name a supplied function.", param="tool_choice", code="invalid_request")
            if set(payload.tool_choice) != {"type", "function"} or set(function) != {"name"}:
                raise OpenAIAPIError(400, "Invalid tool_choice metadata.", param="tool_choice", code="invalid_request")
        elif payload.tool_choice not in (None, "none", "auto", "required"):
            raise OpenAIAPIError(400, "Invalid tool_choice.", param="tool_choice", code="invalid_request")
    pending = set()
    seen = set()
    for message in payload.messages:
        if message.role == "tool":
            if message.tool_call_id not in pending:
                raise OpenAIAPIError(400, "Tool result has no matching pending call.", param="messages", code="invalid_request")
            pending.remove(message.tool_call_id)
        else:
            if pending:
                raise OpenAIAPIError(400, "Every tool call needs a result before the next message.", param="messages", code="invalid_request")
            for call in message.tool_calls or []:
                if call.id in seen:
                    raise OpenAIAPIError(400, "Tool call IDs must be unique.", param="messages", code="invalid_request")
                pending.add(call.id)
                seen.add(call.id)
    if pending:
        raise OpenAIAPIError(400, "Tool call results are missing.", param="messages", code="invalid_request")
    if payload.response_format is not None:
        kind = payload.response_format.get("type")
        if kind == "json_schema":
            spec = payload.response_format.get("json_schema")
            if not isinstance(spec, dict) or not spec.get("name") or not isinstance(spec.get("schema"), dict):
                raise OpenAIAPIError(400, "json_schema requires name and schema.", param="response_format", code="invalid_request")
            if (set(payload.response_format) != {"type", "json_schema"}
                or set(spec) - {"name", "schema", "description", "strict"}
                or not isinstance(spec["name"], str) or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", spec["name"]) is None
                or spec.get("strict") is not None and type(spec["strict"]) is not bool
                or spec.get("description") is not None and not isinstance(spec["description"], str)):
                raise OpenAIAPIError(400, "Invalid schema metadata.", param="response_format", code="invalid_request")
            from src.backend_ai.services.openai_schema_service import validate_schema_request
            validate_schema_request(spec["schema"], strict=spec.get("strict") is True)
        elif kind not in ("text", "json_object"):
            raise OpenAIAPIError(400, "Invalid response_format.type.", param="response_format", code="invalid_request")
        elif set(payload.response_format) != {"type"}:
            raise OpenAIAPIError(400, "Invalid response_format metadata.", param="response_format", code="invalid_request")
