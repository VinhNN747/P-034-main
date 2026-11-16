from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.dependencies.openai_auth import get_openai_principal
from src.backend_ai.dtos.openai_responses import ResponseCreateRequest, ResponseObject
from src.backend_ai.gateway.model_alias import resolve_model
from src.backend_ai.routers.openai_errors import OpenAIAPIError, from_runtime_error, from_runtime_response
from src.backend_ai.routers.runtime import execute_runtime_request
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.backend_ai.services.openai_operation_fingerprint import build_openai_fingerprint
from src.backend_ai.services.openai_request_context import ensure_openai_request_id
from src.backend_ai.services.openai_request_policy import resolve_openai_idempotency_key
from src.backend_ai.services.openai_responses_projection import project_response
from src.backend_ai.services.openai_responses_stream import encode_response_event, iter_response_events
from src.backend_ai.services.openai_responses_validation import (
    check_response_output,
    to_chat_request,
    to_runtime_input,
    validate_response_support,
)
from src.backend_ai.services.runtime_operation_service import (
    RuntimeOperationService,
    RuntimeStorageError,
)
from src.shared.config import get_settings
from src.shared.database.session import get_db
from src.shared.dtos.runtime_api import RuntimeAPIError, RuntimeRequestResponse

router = APIRouter(prefix="/v1", tags=["openai-compatible-responses"])
Principal = Annotated[ApplicationPrincipal, Depends(get_openai_principal)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("/responses")
async def create_response(request: Request, principal: Principal, session: DbSession):
    request_id = ensure_openai_request_id(request)
    raw_body = await _read_body(request)
    try:
        parsed = json.loads(raw_body, parse_constant=_reject_non_finite)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise OpenAIAPIError(400, "Request body must be valid finite JSON.", code="invalid_request") from exc
    try:
        payload = ResponseCreateRequest.model_validate(parsed)
    except ValidationError as exc:
        first = exc.errors(include_input=False, include_url=False)[0] if exc.errors() else {}
        param = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        raise OpenAIAPIError(
            400,
            f"Invalid request: {first.get('msg', 'validation error')}",
            code="invalid_request",
            param=param or None,
        ) from exc
    validate_response_support(payload)

    idempotency_key = resolve_openai_idempotency_key(request, get_settings().gateway_openai_idempotency_policy)
    selection = resolve_model(payload.model)
    runtime_payload = to_runtime_input(payload, selection)
    fingerprint = build_openai_fingerprint(
        "openai_responses",
        payload.model_dump(mode="json", by_alias=True, exclude_none=False),
    )
    from src.backend_ai.services.openai_chat_validation import validate_chat_support
    from src.backend_ai.services.openai_response_store import OpenAIResponseStore
    from src.shared.dtos.gateway import ChatMessage
    try:
        existing = await RuntimeOperationService(session).lookup_normalized(principal, idempotency_key, fingerprint)
    except RuntimeStorageError as exc:
        raise OpenAIAPIError(503, "Response operation storage is unavailable.", type="server_error",
            code="runtime_storage_unavailable") from exc
    if payload.previous_response_id and existing is None:
        previous = await OpenAIResponseStore(session).read(principal.application_id, payload.previous_response_id,
            param="previous_response_id")
        history = [ChatMessage.model_validate(message) for message in previous.history_json or []]
        instructions = [message for message in runtime_payload.messages if message.role in {"system", "developer"}]
        new_input = [message for message in runtime_payload.messages if message.role not in {"system", "developer"}]
        runtime_payload = runtime_payload.model_copy(update={"messages": instructions + history + new_input})
        if len(runtime_payload.messages) > 200:
            raise OpenAIAPIError(400, "Response history exceeds 200 messages.", code="invalid_request", param="input")
    if existing is None:
        request.state.openai_payload = to_chat_request(payload, runtime_payload.messages)
        validate_chat_support(request.state.openai_payload)
    if payload.stream and get_settings().gateway_openai_real_streaming_enabled:
        return await _create_live_response(request, principal, session, payload, selection, runtime_payload,
            idempotency_key, request_id, fingerprint)
    try:
        runtime_response = await execute_runtime_request(
            request,
            principal,
            session,
            runtime_payload,
            idempotency_key=idempotency_key,
            request_id=request_id,
            operation_fingerprint=fingerprint,
            output_policy=lambda result: check_response_output(result, payload),
        )
    except RuntimeAPIError as exc:
        raise from_runtime_error(exc, pinned_model=selection.pinned_model) from exc
    if runtime_response.status_code >= 400:
        raise from_runtime_response(runtime_response, pinned_model=selection.pinned_model)

    try:
        receipt = RuntimeRequestResponse.model_validate_json(runtime_response.body)
        model_names = await _model_names_from_snapshot(session, principal, receipt.request_id)
        response = project_response(receipt, payload, model_names=model_names)
    except RuntimeStorageError as exc:
        headers = _response_headers(receipt, runtime_response.headers, payload.stream)
        raise OpenAIAPIError(
            503,
            "The stored Responses model identity could not be read.",
            type="server_error",
            code="runtime_storage_unavailable",
            headers=headers,
        ) from exc
    except (ValidationError, ValueError, TypeError) as exc:
        headers = {name.lower(): value for name, value in runtime_response.headers.items()}
        raise OpenAIAPIError(
            502,
            "The stored Responses result could not be projected safely.",
            type="server_error",
            code="invalid_runtime_response",
            headers=headers,
        ) from exc

    request.state.request_id = receipt.request_id
    headers = _response_headers(receipt, runtime_response.headers, payload.stream)
    if payload.store:
        try:
            await OpenAIResponseStore(session).save(principal.application_id, receipt, response, runtime_payload.messages)
        except OpenAIAPIError as exc:
            exc.headers.update(headers)
            raise
    if payload.stream:
        return StreamingResponse(
            _encoded_events(response),
            media_type="text/event-stream",
            headers=headers,
        )
    return JSONResponse(status_code=200, content=response.model_dump(mode="json"), headers=headers)


@router.get("/responses/{response_id}")
async def retrieve_response(response_id: str, principal: Principal, session: DbSession):
    from src.backend_ai.services.openai_response_store import OpenAIResponseStore
    record = await OpenAIResponseStore(session).read(principal.application_id, response_id)
    return JSONResponse(record.response_json)


@router.delete("/responses/{response_id}")
async def delete_response(response_id: str, principal: Principal, session: DbSession):
    from src.backend_ai.services.openai_response_store import OpenAIResponseStore
    await OpenAIResponseStore(session).delete(principal.application_id, response_id)
    return {"id": response_id, "object": "response.deleted", "deleted": True}


async def _create_live_response(request, principal, session, payload, selection, runtime_payload,
                                idempotency_key, request_id, fingerprint):
    from src.backend_ai.services.openai_response_store import OpenAIResponseStore
    from src.backend_ai.services.openai_responses_live_stream import ResponseStreamEncoder
    from src.backend_ai.services.runtime_stream_service import RuntimeStreamSession

    projected = None
    encoder = None

    async def owner():
        nonlocal projected
        try:
            final = await execute_runtime_request(request, principal, session, runtime_payload,
                idempotency_key=idempotency_key, request_id=request_id, operation_fingerprint=fingerprint,
                output_policy=lambda result: check_response_output(result, payload))
        except RuntimeAPIError as exc:
            raise from_runtime_error(exc, pinned_model=selection.pinned_model) from exc
        if final.status_code < 400:
            receipt = RuntimeRequestResponse.model_validate_json(final.body)
            names = await _model_names_from_snapshot(session, principal, receipt.request_id)
            projected = project_response(receipt, payload, model_names=names)
            if payload.store:
                await OpenAIResponseStore(session).save(principal.application_id, receipt, projected, runtime_payload.messages)
        return final

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
        headers = _response_headers(receipt, buffered.headers, True)
        headers.update({"cache-control": "no-cache", "x-accel-buffering": "no",
            "x-gateway-streaming-mode": "replay" if buffered.headers.get("idempotency-replayed") == "true" else "buffered"})
        return StreamingResponse(_encoded_events(projected), media_type="text/event-stream", headers=headers)
    encoder = ResponseStreamEncoder(stream, payload)

    async def events():
        try:
            for event in encoder.initial():
                yield event
            async for delta in stream.events():
                for event in encoder.delta(delta):
                    yield event
            final = await stream.final_response()
            if final.status_code >= 400:
                raise from_runtime_response(final, pinned_model=selection.pinned_model)
            for event in encoder.finish(projected):
                yield event
        except OpenAIAPIError as exc:
            yield encoder.failure(exc.code or 'server_error', exc.message)
        except Exception:
            yield encoder.event("error", code="stream_error", message="The response stream could not finish safely.", param=None)
        finally:
            await stream.aclose()
    headers = {"x-request-id": stream.request_id, "x-gateway-request-id": stream.request_id,
        "idempotency-key": idempotency_key, "idempotency-replayed": "false", "x-gateway-streaming-mode": "live",
        "cache-control": "no-cache", "x-accel-buffering": "no"}
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


async def _model_names_from_snapshot(session, principal: ApplicationPrincipal, request_id: str) -> dict[str, str]:
    operation = await RuntimeOperationService(session).read(principal, request_id)
    if operation is None or not isinstance(operation.config_snapshot_json, dict):
        return {}
    models = operation.config_snapshot_json.get("models")
    if not isinstance(models, list):
        return {}
    return {
        model["id"]: model["name"]
        for model in models
        if isinstance(model, dict)
        and isinstance(model.get("id"), str)
        and isinstance(model.get("name"), str)
    }


async def _read_body(request: Request) -> bytes:
    maximum = get_settings().gateway_openai_max_body_bytes
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise OpenAIAPIError(413, f"Request body exceeds {maximum} bytes.", code="request_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _response_headers(receipt: RuntimeRequestResponse, runtime_headers, stream: bool) -> dict[str, str]:
    request_id = runtime_headers.get("X-Request-ID") or receipt.request_id
    headers = {
        "x-request-id": request_id,
        "x-gateway-request-id": request_id,
        "x-gateway-streaming-mode": "buffered" if stream else "none",
        "x-gateway-usage-complete": str(bool(receipt.usage_details and receipt.usage_details.complete)).lower(),
    }
    for name in ("Idempotency-Key", "Idempotency-Replayed"):
        if value := runtime_headers.get(name):
            headers[name] = value
    return headers


def _reject_non_finite(value: str):
    raise ValueError(f"Invalid JSON numeric constant: {value}")


async def _encoded_events(response: ResponseObject) -> AsyncIterator[str]:
    for event in iter_response_events(response):
        yield encode_response_event(event)
