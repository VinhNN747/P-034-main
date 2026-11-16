"""Errors for the OpenAI-compatible ``/v1`` API, rendered in OpenAI's
``{"error": {...}}`` shape so OpenAI SDKs surface the message and code."""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from src.shared.dtos.runtime_api import RuntimeAPIError, RuntimeErrorEnvelope

_TYPE_BY_STATUS = {
    401: "authentication_error",
    403: "permission_error",
    404: "invalid_request_error",
    409: "conflict_error",
    413: "invalid_request_error",
    429: "rate_limit_error",
}


class OpenAIAPIError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        type: str = "invalid_request_error",
        code: str | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.type = type
        self.code = code
        self.param = param
        self.headers = headers or {}

    def body(self) -> dict:
        return {"error": {"message": self.message, "type": self.type, "param": self.param, "code": self.code}}


def from_runtime_error(exc: RuntimeAPIError, *, pinned_model: str | None = None) -> OpenAIAPIError:
    """Re-shape a runtime error (same status/headers) for OpenAI SDK callers."""
    headers = dict(exc.headers)
    if exc.request_id:
        headers.setdefault("x-request-id", exc.request_id)
        headers.setdefault("x-gateway-request-id", exc.request_id)
    if exc.code == "REQUEST_REJECTED" and any(
        issue.get("code") == "ATTACHMENT_ANALYSIS_FAILED" for issue in (exc.details or {}).get("issues", [])
    ):
        return OpenAIAPIError(400, "One or more attachments could not be analyzed.",
            code="attachment_analysis_failed", param="messages", headers=headers)
    if pinned_model and exc.code == "NO_ELIGIBLE_MODEL":
        return OpenAIAPIError(
            404,
            f"The model '{pinned_model}' does not exist in this application's model pool.",
            code="model_not_found",
            param="model",
            headers=headers,
        )
    code = "invalid_api_key" if exc.status_code == 401 else exc.code.lower()
    error_type = _TYPE_BY_STATUS.get(exc.status_code)
    if error_type is None:
        error_type = "server_error" if exc.status_code >= 500 else "invalid_request_error"
    return OpenAIAPIError(
        exc.status_code,
        exc.message,
        type=error_type,
        code=code,
        param=(exc.details or {}).get("param"),
        headers=headers,
    )


def from_runtime_response(response: JSONResponse, *, pinned_model: str | None = None) -> OpenAIAPIError:
    """Map a persisted runtime error response to the same OpenAI envelope as a new error."""
    headers = {
        name.lower(): value
        for name in (
            "X-Request-ID",
            "X-Gateway-Request-ID",
            "X-Gateway-API-Version",
            "Idempotency-Key",
            "Idempotency-Replayed",
            "Retry-After",
            "X-Should-Retry",
        )
        if (value := response.headers.get(name)) is not None
    }
    try:
        envelope = RuntimeErrorEnvelope.model_validate_json(response.body)
    except (ValidationError, ValueError):
        status_code = response.status_code if 400 <= response.status_code < 600 else 502
        error_type = _TYPE_BY_STATUS.get(status_code, "server_error" if status_code >= 500 else "invalid_request_error")
        return OpenAIAPIError(
            status_code,
            "The gateway could not read the stored error response.",
            type=error_type,
            code="invalid_runtime_response",
            headers=headers,
        )

    error = envelope.error
    return from_runtime_error(
        RuntimeAPIError(
            response.status_code,
            error.code,
            error.message,
            retryable=error.retryable,
            details=error.details,
            headers=headers,
            request_id=envelope.request_id,
        ),
        pinned_model=pinned_model,
    )


async def openai_error_handler(request: Request, exc: OpenAIAPIError) -> JSONResponse:
    headers = dict(exc.headers)
    if exc.status_code not in (408, 429):
        headers.setdefault("x-should-retry", "false")
    request_id = headers.get("x-request-id") or getattr(request.state, "request_id", None)
    if request_id:
        headers.setdefault("x-request-id", request_id)
        headers.setdefault("x-gateway-request-id", request_id)
    idempotency_key = getattr(request.state, "idempotency_key", None)
    if idempotency_key:
        headers.setdefault("idempotency-key", idempotency_key)
        headers.setdefault("idempotency-replayed", "false")
    return JSONResponse(status_code=exc.status_code, content=exc.body(), headers=headers)


def is_openai_path(request: Request) -> bool:
    return request.url.path == "/v1" or request.url.path.startswith("/v1/")


def validation_error_to_openai(exc: RequestValidationError) -> OpenAIAPIError:
    first = exc.errors()[0] if exc.errors() else {}
    location = [str(part) for part in first.get("loc", ()) if part not in ("body", "query")]
    return OpenAIAPIError(
        400,
        f"Invalid request: {first.get('msg', 'validation error')}",
        param=".".join(location) or None,
        code="invalid_request",
    )
