from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.dependencies.openai_auth import get_openai_principal
from src.backend_ai.dtos.openai_embeddings import EmbeddingRequest, EmbeddingResponse
from src.backend_ai.routers.openai_errors import OpenAIAPIError, from_runtime_error, from_runtime_response
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.backend_ai.services.embedding_execution_service import EmbeddingExecutionService
from src.backend_ai.services.openai_request_context import ensure_openai_request_id
from src.backend_ai.services.openai_request_policy import resolve_openai_idempotency_key
from src.shared.config import get_settings
from src.shared.database.session import get_db
from src.shared.dtos.runtime_api import RuntimeAPIError

router = APIRouter(prefix="/v1", tags=["openai-compatible-embeddings"])
Principal = Annotated[ApplicationPrincipal, Depends(get_openai_principal)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("/embeddings")
async def create_embedding(request: Request, principal: Principal, session: DbSession):
    request_id = ensure_openai_request_id(request)
    raw_body = await _read_body(request)
    try:
        parsed = json.loads(raw_body, parse_constant=_reject_non_finite)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise OpenAIAPIError(400, "Request body must be valid finite JSON.", code="invalid_request") from exc
    try:
        payload = EmbeddingRequest.model_validate(parsed)
    except ValidationError as exc:
        raise _openai_validation_error(exc) from exc
    idempotency_key = resolve_openai_idempotency_key(request, get_settings().gateway_openai_idempotency_policy)
    try:
        response = await EmbeddingExecutionService(session).execute(
            request,
            principal,
            payload,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    except RuntimeAPIError as exc:
        raise from_runtime_error(exc) from exc
    if response.status_code >= 400:
        raise from_runtime_response(response)
    try:
        body = EmbeddingResponse.model_validate_json(response.body).model_dump(mode="json")
    except (ValidationError, ValueError) as exc:
        raise OpenAIAPIError(502, "The stored embedding response could not be read.",
                             type="server_error", code="invalid_runtime_response",
                             headers={name.lower(): value for name, value in response.headers.items()}) from exc
    return JSONResponse(status_code=200, content=body, headers=dict(response.headers))


async def _read_body(request: Request) -> bytes:
    maximum = get_settings().gateway_embeddings_max_body_bytes
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise OpenAIAPIError(413, f"Request body exceeds {maximum} bytes.", code="request_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _reject_non_finite(value: str):
    raise ValueError(f"Invalid JSON numeric constant: {value}")


def _openai_validation_error(exc: ValidationError) -> OpenAIAPIError:
    first = exc.errors(include_input=False, include_url=False)[0] if exc.errors() else {}
    location = [str(part) for part in first.get("loc", ()) if part not in ("body",)]
    return OpenAIAPIError(
        400,
        f"Invalid request: {first.get('msg', 'validation error')}",
        code="invalid_request",
        param=".".join(location) or None,
    )
