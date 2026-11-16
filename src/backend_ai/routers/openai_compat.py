"""OpenAI-compatible API: point any OpenAI SDK at ``<ai-service>/v1`` with an
application API key and ``model="auto"`` (or an alias, see
``src.backend_ai.gateway.model_alias``) to get gateway routing.

    client = OpenAI(base_url="https://gateway/v1", api_key="ak_...")
    client.chat.completions.create(model="auto:cheap", messages=[...])

This is a thin adapter over the runtime API: the request is translated into a
``RuntimeRequestInput`` and executed by ``execute_runtime_request``, so it
shares idempotency (``Idempotency-Key`` header), rate/concurrency limits,
deadlines, receipts, persistence and billing with ``POST /runtime/requests``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.dependencies.openai_auth import get_openai_principal
from src.backend_ai.dtos.openai_compat import (
    ChatCompletion,
    ChatCompletionChoice,
    ChatCompletionMessage,
    ChatCompletionRequest,
    CompletionUsage,
    ModelCard,
    ModelList,
)
from src.backend_ai.gateway.model_alias import ModelSelection, resolve_model
from src.backend_ai.routers.openai_errors import OpenAIAPIError, from_runtime_error, from_runtime_response
from src.backend_ai.routers.runtime import execute_runtime_request
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.backend_ai.services.openai_chat_validation import validate_chat_support
from src.backend_ai.services.openai_model_service import OpenAIModelService
from src.backend_ai.services.openai_operation_fingerprint import OperationFingerprint, build_openai_fingerprint
from src.backend_ai.services.openai_request_context import ensure_openai_request_id
from src.backend_ai.services.openai_request_policy import resolve_openai_idempotency_key
from src.backend_ai.services.runtime_operation_service import RuntimeOperationService
from src.shared.config import get_settings
from src.shared.database.session import get_db
from src.shared.dtos.runtime_api import (
    RequestConstraints,
    RuntimeAPIError,
    RuntimeRequestInput,
    RuntimeRequestResponse,
)
from src.shared.models.gateway import Model

router = APIRouter(prefix="/v1", tags=["openai-compatible"])
Principal = Annotated[ApplicationPrincipal, Depends(get_openai_principal)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("/models", response_model=ModelList)
async def list_models(principal: Principal, session: DbSession) -> ModelList:
    """Return the aliases and active concrete models visible to this application."""
    return await OpenAIModelService(session).list_visible(principal)


@router.get("/models/{model:path}", response_model=ModelCard)
async def retrieve_model(model: str, principal: Principal, session: DbSession) -> ModelCard:
    """Retrieve a model card from the same visibility set as ``GET /models``."""
    return await OpenAIModelService(session).retrieve_visible(principal, model)


@router.post("/chat/completions")
async def create_chat_completion(request: Request, principal: Principal, session: DbSession):
    request_id = ensure_openai_request_id(request)
    payload = await _parse_body(request)
    _reject_unsupported(payload)
    from src.backend_ai.services.openai_message_execution import requires_message_execution
    if requires_message_execution(payload):
        request.state.openai_payload = payload
    selection = resolve_model(payload.model)

    idempotency_key = resolve_openai_idempotency_key(request, get_settings().gateway_openai_idempotency_policy)

    runtime_payload = _to_runtime_payload(payload, selection)
    legacy_payload = runtime_payload.model_copy(update={"metadata": {
        key: value for key, value in runtime_payload.metadata.items() if key != "openai_hard_completion_token_limit"
    }})
    fingerprint = build_openai_fingerprint("openai_chat_completions", payload.model_dump(mode="json"))
    legacy_fingerprint = OperationFingerprint(RuntimeOperationService.fingerprint(legacy_payload), 1)
    if (payload.tools or payload.n != 1 or any(getattr(payload, name) is not None for name in
        ('temperature', 'top_p', 'stop', 'seed', 'frequency_penalty', 'presence_penalty', 'parallel_tool_calls'))
        or payload.response_format and payload.response_format.get('type') == 'json_schema'
        or any(message.tool_calls or message.tool_call_id for message in payload.messages)):
        # v1 never bound these new producer options. Its hash can only replay
        # requests within the original profile, not silently ignore changes.
        legacy_fingerprint = None
    if payload.stream and get_settings().gateway_openai_real_streaming_enabled:
        return await _create_live_stream(request, principal, session, payload, selection, runtime_payload,
            idempotency_key, request_id, fingerprint, legacy_fingerprint)
    try:
        response = await execute_runtime_request(
            request,
            principal,
            session,
            runtime_payload,
            idempotency_key=idempotency_key,
            request_id=request_id,
            operation_fingerprint=fingerprint,
            legacy_operation_fingerprint=legacy_fingerprint,
            output_policy=lambda result: _check_chat_output(result, payload),
        )
    except RuntimeAPIError as exc:
        raise from_runtime_error(exc, pinned_model=selection.pinned_model) from exc

    if response.status_code >= 400:
        raise from_runtime_response(response, pinned_model=selection.pinned_model)

    result = RuntimeRequestResponse.model_validate_json(response.body)
    request.state.request_id = result.request_id
    names = await _model_names(session, result.models_used)
    completion = _to_chat_completion(result, selection, names)
    headers = _gateway_headers(result, selection, response.headers, names)
    if payload.stream:
        include_usage = bool(payload.stream_options and payload.stream_options.include_usage)
        return StreamingResponse(_stream(completion, include_usage), media_type="text/event-stream", headers=headers)
    return JSONResponse(completion.model_dump(mode="json"), headers=headers)


async def _parse_body(request: Request) -> ChatCompletionRequest:
    limit = get_settings().gateway_openai_max_body_bytes
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise OpenAIAPIError(
                413,
                f"Request body exceeds {limit} bytes; upload large files via /v1/files and send file_id.",
                code="request_too_large",
            )
        chunks.append(chunk)
    try:
        return ChatCompletionRequest.model_validate_json(b"".join(chunks))
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0] if exc.errors() else {}
        param = ".".join(str(part) for part in first.get("loc", ()))
        raise OpenAIAPIError(
            400, f"Invalid request: {first.get('msg', 'validation error')}", param=param or None, code="invalid_request"
        ) from exc


def _reject_unsupported(payload: ChatCompletionRequest) -> None:
    validate_chat_support(payload)


def _to_runtime_payload(payload: ChatCompletionRequest, selection: ModelSelection) -> RuntimeRequestInput:
    options = payload.gateway
    response_format = (payload.response_format or {}).get("type")
    constraint_values: dict[str, Any] = {
        "quality_priority": selection.quality_priority,
        "max_output_tokens": payload.max_completion_tokens or payload.max_tokens,
        "output_format": "json" if response_format == "json_object" else None,
        "allowed_models": [selection.pinned_model] if selection.pinned_model else None,
        **options.model_dump(exclude_none=True),
    }
    metadata: dict[str, Any] = {"api_surface": "openai_chat_completions", "model_alias": selection.requested}
    metadata["openai_hard_completion_token_limit"] = payload.max_completion_tokens or payload.max_tokens
    if payload.user:
        metadata["end_user"] = payload.user
    if payload.metadata:
        metadata["client_metadata"] = payload.metadata
    try:
        return RuntimeRequestInput(
            messages=payload.messages,
            context={},
            constraints=RequestConstraints.model_validate(
                {key: value for key, value in constraint_values.items() if value is not None}
            ),
            metadata=metadata,
        )
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0] if exc.errors() else {}
        raise OpenAIAPIError(
            400, f"Invalid request: {first.get('msg', 'validation error')}", code="invalid_request"
        ) from exc


def _usage(result: RuntimeRequestResponse) -> CompletionUsage | None:
    from src.backend_ai.services.runtime_response_service import public_generation_usage
    scope = public_generation_usage(result)
    if scope is None:
        return None
    prompt_tokens = scope.input_tokens
    completion_tokens = scope.output_tokens if scope else 0
    return CompletionUsage(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=prompt_tokens + completion_tokens
    )


async def _model_names(session: AsyncSession, model_ids: list[str]) -> list[str]:
    """The runtime reports catalog model ids; OpenAI clients expect names."""
    if not model_ids:
        return []
    rows = await session.execute(select(Model.id, Model.name).where(Model.id.in_(model_ids)))
    by_id = dict(rows.all())
    return [by_id.get(model_id, model_id) for model_id in model_ids]


async def _create_live_stream(request, principal, session, payload, selection, runtime_payload,
                              idempotency_key, request_id, fingerprint, legacy_fingerprint):
    from src.backend_ai.services.runtime_stream_service import RuntimeStreamSession

    async def owner():
        try:
            return await execute_runtime_request(request, principal, session, runtime_payload,
                idempotency_key=idempotency_key, request_id=request_id, operation_fingerprint=fingerprint,
                legacy_operation_fingerprint=legacy_fingerprint, output_policy=lambda result: _check_chat_output(result, payload))
        except RuntimeAPIError as exc:
            raise from_runtime_error(exc, pinned_model=selection.pinned_model) from exc

    stream = RuntimeStreamSession().start(owner)
    try:
        buffered = await stream.prime()
    except BaseException:
        await stream.aclose()
        raise
    if buffered is not None:
        if buffered.status_code >= 400:
            raise from_runtime_response(buffered, pinned_model=selection.pinned_model)
        receipt = RuntimeRequestResponse.model_validate_json(buffered.body)
        completion = _to_chat_completion(receipt, selection, await _model_names(session, receipt.models_used))
        headers = _gateway_headers(receipt, selection, buffered.headers, [completion.model])
        headers.update({"x-gateway-streaming-mode": "replay" if buffered.headers.get("idempotency-replayed") == "true"
            else "buffered", "cache-control": "no-cache", "x-accel-buffering": "no"})
        return StreamingResponse(_stream(completion, bool(payload.stream_options and payload.stream_options.include_usage)),
            media_type="text/event-stream", headers=headers)
    import time
    base = {"id": stream.request_id, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": stream.model or selection.requested}
    headers = {"x-request-id": stream.request_id, "x-gateway-request-id": stream.request_id,
        "idempotency-key": idempotency_key, "idempotency-replayed": "false", "x-gateway-streaming-mode": "live",
        "cache-control": "no-cache", "x-accel-buffering": "no"}

    async def events():
        try:
            yield _sse({**base, "choices": [{"index": index, "delta": {"role": "assistant"},
                "finish_reason": None} for index in range(payload.n)]})
            async for event in stream.events():
                delta = {"tool_calls": [event.tool_delta]} if event.type == "tool_delta" else {
                    "refusal" if event.type == "refusal_delta" else "content": event.text}
                yield _sse({**base, "choices": [{"index": event.index, "delta": delta, "finish_reason": None}]})
            final = await stream.final_response()
            if final.status_code >= 400:
                raise from_runtime_response(final, pinned_model=selection.pinned_model)
            receipt = RuntimeRequestResponse.model_validate_json(final.body)
            completion = _to_chat_completion(receipt, selection, [base["model"]])
            yield _sse({**base, "choices": [{"index": choice.index, "delta": {}, "finish_reason": choice.finish_reason}
                for choice in completion.choices]})
            if payload.stream_options and payload.stream_options.include_usage:
                yield _sse({**base, "choices": [], "usage": completion.usage.model_dump() if completion.usage else None})
            yield "data: [DONE]\n\n"
        except OpenAIAPIError as exc:
            yield _sse(exc.body())
        finally:
            await stream.aclose()
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


def _to_chat_completion(result: RuntimeRequestResponse, selection: ModelSelection, names: list[str]) -> ChatCompletion:
    model = names[0] if len(names) == 1 else selection.requested
    if result.output_evidence is None or result.output_evidence.finish_reason == "unknown":
        raise OpenAIAPIError(502, "The provider did not report a supported finish reason.",
            type="server_error", code="unknown_finish_reason")
    return ChatCompletion(
        id=result.request_id,
        created=int((result.finished_at or result.started_at).timestamp()),
        model=model,
        choices=[ChatCompletionChoice.model_validate(choice) for choice in result.openai_choices] if result.openai_choices else [ChatCompletionChoice(
            message=ChatCompletionMessage(content=result.output),
            finish_reason=(result.output_evidence.finish_reason if result.output_evidence else "unknown"),
        )],
        usage=_usage(result),
        gateway_warnings=[warning.code for warning in result.warnings],
    )


def _check_chat_output(result, payload):
    from src.backend_ai.services.openai_schema_service import validate_output_schema
    from src.shared.dtos.runtime_api import RuntimeOperationError, RuntimeOutputPolicy
    if result.aggregation is None or result.aggregation.status.value != "complete":
        return RuntimeOutputPolicy()
    evidence = result.output_evidence
    if evidence is None or evidence.finish_reason == "unknown":
        return RuntimeOutputPolicy(error=RuntimeOperationError(code="UNKNOWN_FINISH_REASON",
            message="The provider did not report a supported finish reason."))
    choices = result.openai_choices or [{"message": {"content": result.aggregation.final_answer},
                                        "finish_reason": evidence.finish_reason, "index": 0}]
    if len(choices) != payload.n:
        return RuntimeOutputPolicy(error=RuntimeOperationError(code="INVALID_PROVIDER_RESPONSE",
            message="The provider returned an unexpected number of choices."))
    def invalid(message):
        return RuntimeOutputPolicy(error=RuntimeOperationError(code="INVALID_PROVIDER_RESPONSE", message=message))
    if not isinstance(choices, list) or any(not isinstance(choice, dict) for choice in choices):
        return invalid("The provider returned invalid choices.")
    if any(type(choice.get('index')) is not int for choice in choices) or [choice.get("index") for choice in choices] != list(range(payload.n)):
        return invalid("The provider returned invalid choice indexes.")
    functions = {tool["function"]["name"]: tool["function"] for tool in payload.tools or []}
    for choice in choices:
        if choice.get("finish_reason") not in {"stop", "length", "content_filter", "tool_calls"}:
            return RuntimeOutputPolicy(error=RuntimeOperationError(code="UNKNOWN_FINISH_REASON",
                message="The provider did not report a supported finish reason."))
        message = choice.get("message")
        if not isinstance(message, dict):
            return invalid("The provider returned an invalid assistant message.")
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            return invalid("The provider returned invalid tool calls.")
        if message.get("role", "assistant") != "assistant" or message.get("content") is not None and not isinstance(message["content"], str):
            return invalid("The provider returned an invalid assistant message.")
        if message.get("refusal") is not None and not isinstance(message["refusal"], str):
            return invalid("The provider returned an invalid refusal.")
        if choice["finish_reason"] == "tool_calls" and not calls:
            return invalid("The provider reported tool calls without any calls.")
        if calls and payload.tool_choice == "none":
            return invalid("The provider returned a tool call when tool_choice is none.")
        if choice["finish_reason"] in {"stop", "tool_calls"} and not message.get("refusal"):
            if payload.tool_choice == "required" and not calls:
                return invalid("The provider did not return a required tool call.")
            expected = payload.tool_choice.get("function", {}).get("name") if isinstance(payload.tool_choice, dict) else None
            ids = set()
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(function, dict) or call.get("type") != "function" or function.get("name") not in functions:
                    return invalid("The provider returned an unknown function.")
                if not isinstance(call.get("id"), str) or not call["id"] or call["id"] in ids:
                    return invalid("The provider returned invalid tool call IDs.")
                ids.add(call["id"])
                if expected and function["name"] != expected:
                    return invalid("The provider did not select the requested function.")
                schema = functions[function["name"]].get("parameters") or {"type": "object"}
                if not validate_output_schema(function.get("arguments"), schema).valid:
                    return invalid("The provider returned invalid function arguments.")
        if choice["finish_reason"] == "stop" and not message.get("refusal") and payload.response_format:
            kind = payload.response_format.get("type")
            schema = payload.response_format["json_schema"]["schema"] if kind == "json_schema" else {"type": "object"}
            if kind in {"json_schema", "json_object"} and not validate_output_schema(message.get("content"), schema).valid:
                return RuntimeOutputPolicy(error=RuntimeOperationError(code="INVALID_PROVIDER_RESPONSE",
                    message="The final output does not satisfy the requested JSON format.", details={"param": "response_format"}))
    return RuntimeOutputPolicy()


def _gateway_headers(
    result: RuntimeRequestResponse, selection: ModelSelection, runtime_headers, names: list[str]
) -> dict[str, str]:
    headers = {
        "x-request-id": result.request_id,
        "x-gateway-request-id": result.request_id,
        "x-gateway-model-alias": selection.requested,
        "x-gateway-models": ",".join(names),
        "x-gateway-status": result.status,
        "x-gateway-cost-usd": f"{result.cost.estimated_cost:.8f}" if result.cost else "0",
    }
    for name in ("Idempotency-Key", "Idempotency-Replayed"):
        value = runtime_headers.get(name)
        if value is not None:
            headers[name] = value
    return headers


async def _stream(completion: ChatCompletion, include_usage: bool) -> AsyncIterator[str]:
    """The answer only exists after routing/aggregation finishes, so the
    stream carries it in one content chunk; clients using stream=True still
    work unchanged."""
    base = {"id": completion.id, "object": "chat.completion.chunk", "created": completion.created, "model": completion.model}
    for choice in completion.choices:
        delta = {"role": "assistant", "content": choice.message.content}
        if choice.message.tool_calls:
            delta["tool_calls"] = [{"index": index, **tool} for index, tool in enumerate(choice.message.tool_calls)]
        if choice.message.refusal:
            delta["refusal"] = choice.message.refusal
        yield _sse({**base, "choices": [{"index": choice.index, "delta": delta, "finish_reason": None}]})
    yield _sse({**base, "choices": [{"index": choice.index, "delta": {}, "finish_reason": choice.finish_reason}
        for choice in completion.choices]})
    if include_usage:
        yield _sse({**base, "choices": [], "usage": completion.usage.model_dump() if completion.usage else None})
    yield "data: [DONE]\n\n"


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
