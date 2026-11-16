from __future__ import annotations

import re
from collections.abc import Mapping

from src.backend_ai.dtos.openai_responses import (
    ResponseCreateRequest,
    ResponseFunctionCall,
    ResponseIncompleteDetails,
    ResponseMeasuredUsage,
    ResponseObject,
    ResponseOutputMessage,
    ResponseOutputTextPart,
    ResponseRefusalPart,
    ResponseTextConfig,
    ResponseTextFormatText,
    ResponseUsage,
    ResponseUsageInputDetails,
    ResponseUsageOutputDetails,
)
from src.shared.dtos.gateway import OutputEvidence
from src.shared.dtos.runtime_api import RuntimeRequestResponse, RuntimeUsageDetails


def project_response(
    receipt: RuntimeRequestResponse,
    payload: ResponseCreateRequest,
    *,
    model_names: Mapping[str, str] | None = None,
) -> ResponseObject:
    """Create a stable Responses object from its durable runtime receipt."""
    if receipt.status != "completed" or not isinstance(receipt.output, str):
        raise ValueError("Only completed text receipts can be projected as a Responses object")

    evidence = receipt.output_evidence or OutputEvidence()
    status = "completed" if evidence.finish_reason in {"stop", "tool_calls"} else "incomplete"
    incomplete_reason = {
        "length": "max_output_tokens",
        "content_filter": "content_filter",
    }.get(evidence.finish_reason)
    from src.backend_ai.services.runtime_response_service import public_generation_usage
    scope = public_generation_usage(receipt)
    usage = _response_usage(scope, RuntimeUsageDetails(
        cached_tokens=evidence.cached_tokens, cache_write_tokens=evidence.cache_write_tokens,
        reasoning_tokens=evidence.reasoning_tokens,
        complete=all(value is not None for value in (
            evidence.cached_tokens, evidence.cache_write_tokens, evidence.reasoning_tokens)),
    ))
    normalized_id = re.sub(r"[^a-zA-Z0-9]", "", receipt.request_id)
    text_config_values = {"format": payload.text.format or ResponseTextFormatText(type="text")}
    if payload.text.verbosity is not None:
        text_config_values["verbosity"] = payload.text.verbosity
    text_config = ResponseTextConfig(**text_config_values)
    output = ResponseOutputMessage(
        id=f"msg_{normalized_id}_0",
        status=status,
        content=[ResponseOutputTextPart(type="output_text", text=receipt.output)],
    )
    outputs = [output]
    if receipt.openai_choices:
        message = receipt.openai_choices[0]["message"]
        outputs = []
        if message.get("content") is not None or message.get("refusal"):
            content = [ResponseRefusalPart(refusal=message["refusal"])] if message.get("refusal") else [
                ResponseOutputTextPart(type="output_text", text=message["content"])]
            outputs.append(output.model_copy(update={"content": content}))
        for index, call in enumerate(message.get("tool_calls") or []):
            outputs.append(ResponseFunctionCall(type="function_call", id=f"fc_{normalized_id}_{index}",
                call_id=call["id"], name=call["function"]["name"], arguments=call["function"]["arguments"], status=status))
    if receipt.openai_output_order:
        by_key = {'message' if item.type == 'message' else 'function_call:' + item.id.rsplit('_',1)[1]: item for item in outputs}
        if set(by_key) != set(receipt.openai_output_order):
            raise ValueError('Stored stream output order does not match final items')
        outputs = [by_key[key] for key in receipt.openai_output_order]
    return ResponseObject(
        id=f"resp_{normalized_id}",
        created_at=float(receipt.started_at.timestamp()),
        incomplete_details=(ResponseIncompleteDetails(reason=incomplete_reason) if status == "incomplete" else None),
        instructions=payload.instructions,
        metadata=payload.metadata,
        model=(model_names or {}).get(receipt.models_used[0], payload.model) if receipt.models_used else payload.model,
        output=outputs,
        status=status,
        store=bool(payload.store),
        previous_response_id=payload.previous_response_id,
        max_output_tokens=payload.max_output_tokens,
        temperature=payload.temperature,
        top_p=payload.top_p,
        tools=payload.tools or [],
        tool_choice=payload.tool_choice or ("auto" if payload.tools else "none"),
        parallel_tool_calls=bool(payload.parallel_tool_calls),
        gateway_warnings=[warning.code for warning in receipt.warnings],
        text=text_config,
        usage=usage,
        gateway_usage=ResponseMeasuredUsage(
            input_tokens=scope.input_tokens, output_tokens=scope.output_tokens, total_tokens=scope.total_tokens,
            cached_tokens=evidence.cached_tokens, cache_write_tokens=evidence.cache_write_tokens,
            reasoning_tokens=evidence.reasoning_tokens,
        ) if scope is not None else None,
    )


def _response_usage(scope, details: RuntimeUsageDetails | None) -> ResponseUsage | None:
    if scope is None or details is None or not details.complete:
        return None
    if details.cached_tokens is None or details.cache_write_tokens is None or details.reasoning_tokens is None:
        return None
    if scope.total_tokens != scope.input_tokens + scope.output_tokens:
        return None
    if details.cached_tokens + details.cache_write_tokens > scope.input_tokens:
        return None
    if details.reasoning_tokens > scope.output_tokens:
        return None
    return ResponseUsage(
        input_tokens=scope.input_tokens,
        input_tokens_details=ResponseUsageInputDetails(
            cached_tokens=details.cached_tokens,
            cache_write_tokens=details.cache_write_tokens,
        ),
        output_tokens=scope.output_tokens,
        output_tokens_details=ResponseUsageOutputDetails(reasoning_tokens=details.reasoning_tokens),
        total_tokens=scope.total_tokens,
    )
