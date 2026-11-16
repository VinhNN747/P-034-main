from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.services.application_auth_service import (
    ApplicationKeyAuthService,
    ApplicationPrincipal,
    InactiveApplicationError,
    InvalidApplicationKeyError,
)
from src.backend_ai.services.comparison_service import ComparisonExecutionService, ComparisonValidationError
from src.backend_ai.services.gateway_execution_service import GatewayExecutionService
from src.backend_ai.services.gateway_routing_service import RoutingError
from src.backend_ai.services.openai_operation_fingerprint import OperationFingerprint
from src.backend_ai.services.runtime_operation_service import (
    InvalidIdempotencyKeyError,
    RuntimeOperationService,
    RuntimeStorageError,
)
from src.backend_ai.services.runtime_request_limits import MAX_RUNTIME_METADATA_BYTES
from src.backend_ai.services.runtime_response_service import RuntimeResponseService
from src.shared.config import get_settings
from src.shared.database.session import get_db
from src.shared.dtos.gateway import PipelineResult
from src.shared.dtos.runtime_api import (
    RuntimeAPIError,
    RuntimeOperationError,
    RuntimeOutputPolicy,
    RuntimeRequestInput,
    RuntimeWarning,
)
from src.shared.models.runtime import RuntimeOperationRecord
from src.shared.services.application_budget_service import (
    ApplicationBudgetService,
    BudgetExceededError,
)
from src.shared.services.budget_alert_service import BudgetAlertService
from src.shared.services.comparison_query_service import ComparisonNotFoundError, ComparisonQueryService
from src.shared.services.gateway_persistence_service import GatewayPersistenceService

router = APIRouter(prefix="/runtime", tags=["gateway-runtime-api"])
logger = logging.getLogger("uvicorn.error")
_bearer = HTTPBearer(auto_error=False)
_MAX_BODY_BYTES = 256 * 1024
_MAX_CONTEXT_BYTES = 64 * 1024
_MAX_METADATA_BYTES = MAX_RUNTIME_METADATA_BYTES


def _gateway_execution_service(request: Request, session: AsyncSession) -> GatewayExecutionService:
    return GatewayExecutionService(
        session,
        get_settings(),
        latency_runtime=getattr(request.app.state, "latency_runtime", None),
    )


async def _runtime_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ApplicationPrincipal:
    # Keep the feature flag ahead of credential parsing/authentication: disabled
    # deployments expose one stable response even when callers omit a key.
    if not get_settings().gateway_runtime_enabled:
        raise RuntimeAPIError(503, "RUNTIME_DISABLED", "The gateway runtime is not enabled.")
    if credentials is None:
        raise RuntimeAPIError(
            401,
            "AUTHENTICATION_FAILED",
            "A valid gateway API key is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await ApplicationKeyAuthService(session).authenticate(credentials.credentials)
    except InvalidApplicationKeyError as exc:
        raise RuntimeAPIError(
            401,
            "AUTHENTICATION_FAILED",
            "A valid gateway API key is required.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except InactiveApplicationError as exc:
        raise RuntimeAPIError(403, "APPLICATION_INACTIVE", "The application is inactive.") from exc
    except SQLAlchemyError as exc:
        await session.rollback()
        raise RuntimeAPIError(
            503,
            "RUNTIME_STORAGE_UNAVAILABLE",
            "Gateway authentication storage is unavailable.",
            retryable=True,
        ) from exc


Principal = Annotated[ApplicationPrincipal, Depends(_runtime_principal)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("/requests")
async def create_runtime_request(request: Request, principal: Principal, session: DbSession):
    request_id = str(uuid4())
    request.state.request_id = request_id
    request.state.runtime_api = True

    idempotency_key = request.headers.get("Idempotency-Key") or str(uuid4())
    try:
        RuntimeOperationService.validate_idempotency_key(idempotency_key)
    except InvalidIdempotencyKeyError as exc:
        raise RuntimeAPIError(
            400,
            "INVALID_IDEMPOTENCY_KEY",
            "Idempotency-Key must contain 1 to 128 visible ASCII characters.",
            request_id=request_id,
        ) from exc
    request.state.idempotency_key = idempotency_key

    raw_body = await _read_bounded_body(request, request_id)
    try:
        parsed = json.loads(raw_body, parse_constant=_reject_non_finite)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeAPIError(
            400, "INVALID_JSON", "Request body must be valid finite JSON.", request_id=request_id
        ) from exc
    try:
        payload = RuntimeRequestInput.model_validate(parsed)
    except ValidationError as exc:
        raise _validation_error(exc, request_id) from exc

    return await execute_runtime_request(
        request,
        principal,
        session,
        payload,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )


async def execute_runtime_request(
    request: Request,
    principal: ApplicationPrincipal,
    session: AsyncSession,
    payload: RuntimeRequestInput,
    *,
    idempotency_key: str,
    request_id: str,
    operation_fingerprint: OperationFingerprint | None = None,
    legacy_operation_fingerprint: OperationFingerprint | None = None,
    output_policy: Callable[[PipelineResult], RuntimeOutputPolicy] | None = None,
) -> JSONResponse:
    """Idempotent, metered execution of one validated runtime payload.

    Shared by ``POST /runtime/requests`` and the OpenAI-compatible
    ``POST /v1/chat/completions`` adapter, so both get the same idempotency,
    rate limits, deadlines, receipts, persistence and billing. Returns the
    public RuntimeResponse JSON (200) or raises RuntimeAPIError.
    """
    _validate_json_size(payload.context, _MAX_CONTEXT_BYTES, "context", request_id)
    _validate_json_size(payload.metadata, _MAX_METADATA_BYTES, "metadata", request_id)
    operation_service = RuntimeOperationService(
        session,
        requests_per_minute=get_settings().gateway_runtime_requests_per_minute,
        max_concurrent_requests=get_settings().gateway_runtime_max_concurrent_requests,
        deadline_seconds=get_settings().gateway_runtime_deadline_seconds,
    )
    try:
        if operation_fingerprint is None:
            existing = await operation_service.lookup(principal, idempotency_key, payload)
        else:
            existing = await operation_service.lookup_normalized(
                principal, idempotency_key, operation_fingerprint
            )
        if (existing is not None and existing.kind == "conflict" and legacy_operation_fingerprint is not None
            and existing.operation.normalization_version == 1):
            legacy = await operation_service.lookup_normalized(principal, idempotency_key, legacy_operation_fingerprint)
            if legacy is not None and legacy.kind != "conflict":
                existing = legacy
    except (RuntimeStorageError, InvalidIdempotencyKeyError) as exc:
        raise RuntimeAPIError(
            503,
            "RUNTIME_STORAGE_UNAVAILABLE" if isinstance(exc, RuntimeStorageError) else "INVALID_IDEMPOTENCY_KEY",
            "Runtime operation could not be loaded." if isinstance(exc, RuntimeStorageError) else str(exc),
            retryable=isinstance(exc, RuntimeStorageError),
            request_id=request_id,
        ) from exc
    if existing is not None:
        return _existing_post_response(existing, idempotency_key, request_id)

    execution_service = _gateway_execution_service(request, session)
    execution_service.openai_payload = getattr(request.state, "openai_payload", None)
    try:
        prepared = await execution_service.prepare_public(principal, payload)
        if execution_service.openai_payload is not None:
            from dataclasses import replace

            from src.backend_ai.services.openai_message_execution import preflight_options
            eligible = preflight_options(execution_service.openai_payload, prepared)
            prepared = replace(prepared, routing_snapshot=replace(prepared.routing_snapshot, candidates=eligible))
            prepared.snapshot_json["openai_profile"] = {"eligible_model_ids": [row[1].id for row in eligible]}
    except RuntimeAPIError:
        raise
    except Exception as exc:
        raise RuntimeAPIError(
            500,
            "INTERNAL_ERROR",
            "The gateway could not prepare this request.",
            request_id=request_id,
        ) from exc
    # A few isolated execution tests use a deliberately minimal session stub;
    # real request sessions always expose ``scalar`` through AsyncSession.
    if hasattr(session, "scalar"):
        try:
            budget = await ApplicationBudgetService(session).ensure_can_execute(principal.application_id)
            prepared.snapshot_json["budget"] = budget.as_dict()
        except BudgetExceededError as exc:
            raise RuntimeAPIError(
                429,
                exc.code,
                str(exc),
                retryable=False,
                details=exc.snapshot.as_dict(),
                request_id=request_id,
            ) from exc
    try:
        if operation_fingerprint is None:
            claim = await operation_service.claim(
                principal,
                idempotency_key,
                payload,
                config_version=prepared.config.version,
                config_snapshot=prepared.snapshot_json,
                execution_mode=get_settings().gateway_execution_mode,
            )
        else:
            claim = await operation_service.claim_normalized(
                principal,
                idempotency_key,
                operation_fingerprint,
                config_version=prepared.config.version,
                config_snapshot=prepared.snapshot_json,
                execution_mode=get_settings().gateway_execution_mode,
            )
    except RuntimeStorageError as exc:
        raise RuntimeAPIError(
            503,
            "RUNTIME_STORAGE_UNAVAILABLE",
            "Runtime operation could not be claimed.",
            retryable=True,
            request_id=request_id,
        ) from exc
    if claim.kind != "owner":
        return _existing_post_response(claim, idempotency_key, request_id)

    operation = claim.operation
    if operation is None:
        raise RuntimeAPIError(
            503, "RUNTIME_STORAGE_UNAVAILABLE", "Runtime claim returned no operation.", retryable=True
        )
    # Rollback (including a lost commit acknowledgement) expires ORM fields.
    # Keep the claim identity usable without async implicit DB reads.
    operation = SimpleNamespace(**{column.name: deepcopy(getattr(operation, column.name, None))
                                   for column in RuntimeOperationRecord.__table__.columns})
    request.state.request_id = operation.request_id
    request.state.operation_id = operation.request_id
    from src.backend_ai.services.runtime_stream_service import current_stream
    if stream := current_stream():
        stream.request_id = operation.request_id
        stream.pricing = prepared.pricing

    async def finalize_unknown(unknown, body):
        partial = stream.partial_result if stream is not None else None
        if partial is None or not any(trace.model and (trace.input_tokens or trace.output_tokens or trace.cost_usd)
                                      for trace in partial.traces):
            return await _finalize_unknown(operation_service, operation.request_id, unknown, body, 409)
        finished = datetime.now(UTC)
        measured = RuntimeResponseService.from_result(partial, prepared.snapshot_json,
            wall_latency_ms=(finished - _as_utc(operation.started_at)).total_seconds()*1000,
            operation_started_at=operation.started_at, finished_at=finished, execution_mode=operation.execution_mode)
        measured = measured.model_copy(update={'status':'indeterminate','output':None,
            'usage':measured.usage.model_copy(update={'complete':False}),
            'error':RuntimeOperationError(code='REQUEST_OUTCOME_UNKNOWN', message=body['error']['message'])})
        await _persist_and_finalize(session, operation_service,
            RuntimeResponseService.sanitize_for_persistence(partial), measured, operation, principal,
            prepared.config.version, 409, body, status_override='indeterminate')
    deadline_at = time.monotonic() + get_settings().gateway_runtime_deadline_seconds
    try:
        result = await execution_service.run_public(
            principal,
            payload,
            prepared,
            operation.request_id,
            operation.started_at,
            deadline_at,
        )
    except TimeoutError:
        unknown = _unknown_receipt(operation)
        body = _error_body(
            "REQUEST_OUTCOME_UNKNOWN", "The request outcome could not be confirmed.", operation.request_id
        )
        try:
            await finalize_unknown(unknown, body)
        except RuntimeStorageError as exc:
            raise RuntimeAPIError(
                503,
                "RUNTIME_STORAGE_UNAVAILABLE",
                "The request ran but its final receipt could not be confirmed.",
                retryable=True,
                request_id=operation.request_id,
            ) from exc
        raise RuntimeAPIError(
            409,
            "REQUEST_OUTCOME_UNKNOWN",
            "The request outcome could not be confirmed.",
            request_id=operation.request_id,
        )
    except asyncio.CancelledError:
        unknown = _unknown_receipt(operation)
        body = _error_body(
            "REQUEST_OUTCOME_UNKNOWN", "The request outcome could not be confirmed.", operation.request_id
        )
        try:
            await finalize_unknown(unknown, body)
        except Exception:
            await session.rollback()
        raise
    except RoutingError as exc:
        code = "NO_ELIGIBLE_MODEL"
        body = _error_body(code, "No model in the application pool can serve this request.", operation.request_id)
        response = RuntimeResponseService.from_operation(operation).model_copy(
            update={
                "status": "failed",
                "finished_at": datetime.now(UTC),
                "error": RuntimeOperationError(
                    code=code,
                    message=body["error"]["message"],
                    retryable=False,
                    details={},
                ),
            }
        )
        try:
            await _finalize_without_result(
                operation_service,
                operation.request_id,
                response.model_dump(mode="json"),
                body,
                422,
            )
        except RuntimeStorageError as storage_exc:
            raise RuntimeAPIError(
                503,
                "RUNTIME_STORAGE_UNAVAILABLE",
                "The request ran but its final receipt could not be confirmed.",
                retryable=True,
                request_id=operation.request_id,
            ) from storage_exc
        raise RuntimeAPIError(422, code, body["error"]["message"], request_id=operation.request_id) from exc
    except Exception:
        # Once execution has started an unexpected exception cannot establish
        # whether an upstream provider accepted the call. Keep the receipt
        # non-replayable and ask callers to inspect it instead of rerunning.
        unknown = _unknown_receipt(operation)
        body = _error_body(
            "REQUEST_OUTCOME_UNKNOWN", "The request outcome could not be confirmed.", operation.request_id
        )
        try:
            await finalize_unknown(unknown, body)
        except RuntimeStorageError as exc:
            raise RuntimeAPIError(
                503,
                "RUNTIME_STORAGE_UNAVAILABLE",
                "The request ran but its final receipt could not be confirmed.",
                retryable=True,
                request_id=operation.request_id,
            ) from exc
        except Exception:
            await session.rollback()
        raise RuntimeAPIError(
            409,
            "REQUEST_OUTCOME_UNKNOWN",
            "The request outcome could not be confirmed.",
            request_id=operation.request_id,
        )

    finished = datetime.now(UTC)
    elapsed_ms = (finished - _as_utc(operation.started_at)).total_seconds() * 1000
    policy_error = None
    if output_policy is not None:
        try:
            policy_error = output_policy(result).error
        except Exception:
            policy_error = RuntimeOperationError(code='INVALID_PROVIDER_RESPONSE',
                message='The completed provider response could not be validated.')
    if policy_error is not None:
        # Invalid protocol output must not break receipt serialization and
        # discard the measured usage from the completed generation.
        result.openai_choices = None
    response = RuntimeResponseService.from_result(
        result,
        prepared.snapshot_json,
        wall_latency_ms=elapsed_ms,
        operation_started_at=operation.started_at,
        finished_at=finished,
        execution_mode=operation.execution_mode,
    )
    if policy_error is not None:
        response = response.model_copy(update={"status": "failed", "error": policy_error})
    http_status = 200
    error_body = None
    status_override = "failed" if policy_error is not None else None
    if policy_error is not None:
        http_status = 502
        error_body = _error_body(
            policy_error.code,
            policy_error.message,
            operation.request_id,
            retryable=policy_error.retryable,
            details=policy_error.details,
        )
    elif response.status == "failed":
        http_status = 422 if response.error and response.error.code == "REQUEST_REJECTED" else 502
        error_body = _error_body(
            response.error.code if response.error else "AGGREGATION_FAILED",
            response.error.message if response.error else "The request could not be completed.",
            operation.request_id,
            retryable=response.error.retryable if response.error else False,
            details=response.error.details if response.error else {},
        )

    try:
        await _persist_and_finalize(
            session,
            operation_service,
            RuntimeResponseService.sanitize_for_persistence(result),
            response,
            operation,
            principal,
            prepared.config.version,
            http_status,
            error_body,
            status_override=status_override,
        )
    except RuntimeStorageError as exc:
        await session.rollback()
        raise RuntimeAPIError(
            503,
            "RUNTIME_STORAGE_UNAVAILABLE",
            "The request ran but its final receipt could not be confirmed.",
            retryable=True,
            request_id=operation.request_id,
        ) from exc
    if http_status == 200 and payload.comparison is not None:
        try:
            comparison_id = await ComparisonExecutionService(session, get_settings()).create_from_runtime(
                payload,
                response,
                idempotency_key=idempotency_key,
            )
            if comparison_id:
                response = response.model_copy(update={"comparison_id": comparison_id})
        except ComparisonValidationError as exc:
            response = response.model_copy(
                update={
                    "warnings": [
                        *response.warnings,
                        RuntimeWarning(
                            code="COMPARISON_NOT_STARTED",
                            message="The Gateway request completed, but its comparison could not be queued.",
                            details={"reason": str(exc)},
                        ),
                    ]
                }
            )
        try:
            stored_operation = await session.get(RuntimeOperationRecord, operation.request_id)
            if stored_operation is not None:
                stored_operation.public_result_json = response.model_dump(mode="json")
                stored_operation.http_body_json = response.model_dump(mode="json")
                await session.commit()
        except SQLAlchemyError:
            await session.rollback()
        except Exception:
            await session.rollback()
            response = response.model_copy(
                update={
                    "warnings": [
                        *response.warnings,
                        RuntimeWarning(
                            code="COMPARISON_NOT_STARTED",
                            message="The Gateway request completed, but its comparison could not be queued.",
                        ),
                    ]
                }
            )
    if http_status != 200:
        raise RuntimeAPIError(
            http_status,
            response.error.code if response.error else "AGGREGATION_FAILED",
            response.error.message if response.error else "The request could not be completed.",
            retryable=response.error.retryable if response.error else False,
            details=response.error.details if response.error else {},
            request_id=operation.request_id,
        )
    return _json_response(
        response.model_dump(mode="json"),
        request_id=operation.request_id,
        idempotency_key=idempotency_key,
        replayed=False,
    )


@router.get("/requests/{request_id}")
async def get_runtime_request(request_id: str, request: Request, principal: Principal, session: DbSession):
    request.state.runtime_api = True
    request.state.request_id = request_id
    try:
        operation = await RuntimeOperationService(session).read(principal, request_id)
    except RuntimeStorageError as exc:
        raise RuntimeAPIError(
            503,
            "RUNTIME_STORAGE_UNAVAILABLE",
            "Runtime operation could not be loaded.",
            retryable=True,
            request_id=request_id,
        ) from exc
    if operation is None:
        raise RuntimeAPIError(404, "REQUEST_NOT_FOUND", "Request not found.", request_id=request_id)
    response = RuntimeResponseService.from_operation(operation)
    return JSONResponse(
        status_code=200,
        content=response.model_dump(mode="json"),
        headers={"X-Request-ID": request_id, "X-Gateway-API-Version": "1"},
    )


@router.get("/comparisons/{comparison_id}")
async def get_runtime_comparison(
    comparison_id: str,
    request: Request,
    principal: Principal,
    session: DbSession,
):
    request.state.runtime_api = True
    request.state.request_id = comparison_id
    try:
        report = await ComparisonQueryService(session).get_application(comparison_id, principal.application_id)
    except ComparisonNotFoundError as exc:
        raise RuntimeAPIError(404, "COMPARISON_NOT_FOUND", "Comparison not found.", request_id=comparison_id) from exc
    return JSONResponse(
        status_code=200,
        content=report.model_dump(mode="json"),
        headers={"X-Request-ID": comparison_id, "X-Gateway-API-Version": "1"},
    )


async def _read_bounded_body(request: Request, request_id: str) -> bytes:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > _MAX_BODY_BYTES:
                raise RuntimeAPIError(413, "REQUEST_TOO_LARGE", "Request body exceeds 256 KiB.", request_id=request_id)
            if int(length) < 0:
                raise ValueError
        except ValueError as exc:
            raise RuntimeAPIError(400, "INVALID_JSON", "Invalid Content-Length header.", request_id=request_id) from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            raise RuntimeAPIError(413, "REQUEST_TOO_LARGE", "Request body exceeds 256 KiB.", request_id=request_id)
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_json_size(value: dict[str, Any], maximum: int, name: str, request_id: str) -> None:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeAPIError(
            422, "VALIDATION_ERROR", f"{name} must contain finite JSON values.", request_id=request_id
        ) from exc
    if len(encoded) > maximum:
        raise RuntimeAPIError(
            422,
            "VALIDATION_ERROR",
            f"{name} exceeds its allowed size.",
            details={"field": name, "max_bytes": maximum},
            request_id=request_id,
        )


def _validation_error(exc: ValidationError, request_id: str) -> RuntimeAPIError:
    issues = []
    for item in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(part) for part in item.get("loc", []))
        issues.append({"field": loc, "type": item.get("type", "invalid")})
    return RuntimeAPIError(
        422,
        "VALIDATION_ERROR",
        "Request body failed validation.",
        details={"issues": issues},
        request_id=request_id,
    )


def _reject_non_finite(value: str):
    raise ValueError(f"Invalid JSON numeric constant: {value}")


def _existing_post_response(claim, idempotency_key: str, correlation_id: str):
    operation = claim.operation
    if claim.kind == "conflict":
        raise RuntimeAPIError(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key was already used with a different request body.",
            request_id=operation.request_id if operation else correlation_id,
        )
    if claim.kind in {"running", "owner"}:
        request_id = operation.request_id if operation else correlation_id
        raise RuntimeAPIError(
            409,
            "REQUEST_IN_PROGRESS",
            "Request is still running.",
            retryable=True,
            details={"request_id": request_id},
            headers={"Retry-After": str(claim.retry_after or 1)},
            request_id=request_id,
        )
    if claim.kind in {"unknown", "indeterminate"}:
        request_id = operation.request_id if operation else correlation_id
        raise RuntimeAPIError(
            409,
            "REQUEST_OUTCOME_UNKNOWN",
            "The request outcome could not be confirmed.",
            request_id=request_id,
        )
    if claim.kind in {"replay", "terminal_replay"} and operation is not None:
        if claim.kind == "terminal_replay" and operation.http_body_json is not None:
            return JSONResponse(
                status_code=operation.http_status or 502,
                content=operation.http_body_json,
                headers=_runtime_headers(operation.request_id, idempotency_key, replayed=True),
            )
        if claim.kind == "replay" and operation.public_result_json is not None:
            return JSONResponse(
                status_code=200,
                content=operation.public_result_json,
                headers=_runtime_headers(operation.request_id, idempotency_key, replayed=True),
            )
        raise RuntimeAPIError(
            operation.http_status or 502,
            "REQUEST_OUTCOME_UNKNOWN",
            "The stored request receipt is unavailable.",
            request_id=operation.request_id,
        )
    if claim.kind == "rate_limited":
        raise RuntimeAPIError(
            429,
            "RATE_LIMIT_EXCEEDED",
            "Application request rate limit exceeded.",
            retryable=True,
            headers={"Retry-After": str(claim.retry_after or 1)},
            request_id=correlation_id,
        )
    if claim.kind == "concurrency_limited":
        raise RuntimeAPIError(
            429,
            "CONCURRENCY_LIMIT_EXCEEDED",
            "Application concurrent request limit exceeded.",
            retryable=True,
            headers={"Retry-After": str(claim.retry_after or 1)},
            request_id=correlation_id,
        )
    raise RuntimeAPIError(503, "RUNTIME_STORAGE_UNAVAILABLE", "Operation claim could not be resolved.", retryable=True)


def _json_response(content: dict, *, request_id: str, idempotency_key: str, replayed: bool):
    return JSONResponse(
        status_code=200,
        content=content,
        headers=_runtime_headers(request_id, idempotency_key, replayed),
    )


def _runtime_headers(request_id: str, idempotency_key: str, replayed: bool) -> dict[str, str]:
    return {
        "X-Request-ID": request_id,
        "X-Gateway-API-Version": "1",
        "Idempotency-Key": idempotency_key,
        "Idempotency-Replayed": str(replayed).lower(),
    }


def _error_body(code: str, message: str, request_id: str, *, retryable: bool = False, details: dict | None = None):
    return {
        "error": {"code": code, "message": message, "retryable": retryable, "details": details or {}},
        "request_id": request_id,
    }


def _unknown_receipt(operation):
    receipt = RuntimeResponseService.from_operation(operation)
    return receipt.model_copy(
        update={
            "status": "indeterminate",
            "output": None,
            "usage": None,
            "cost": None,
            "latency_ms": None,
            "finished_at": None,
            "error": RuntimeOperationError(
                code="REQUEST_OUTCOME_UNKNOWN",
                message="The request outcome could not be confirmed.",
                retryable=False,
                details={},
            ),
        }
    ).model_dump(mode="json")


async def _finalize_unknown(service, request_id: str, receipt: dict, body: dict, http_status: int) -> None:
    await _finalize_without_result(service, request_id, receipt, body, http_status, status="indeterminate")


async def _finalize_without_result(
    service,
    request_id: str,
    receipt: dict,
    body: dict,
    http_status: int,
    *,
    status: str = "failed",
) -> None:
    try:
        finalized = await service.finalize_in_transaction(
            request_id,
            status=status,
            public_result=receipt,
            http_status=http_status,
            http_body=body,
        )
        if finalized:
            await service.session.commit()
        else:
            await service.session.rollback()
    except Exception:
        await service.session.rollback()
        raise RuntimeStorageError("Unable to finalize the runtime operation")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _persist_and_finalize(
    session: AsyncSession,
    operation_service: RuntimeOperationService,
    result,
    response,
    operation,
    principal: ApplicationPrincipal,
    config_version: int,
    http_status: int,
    error_body: dict | None,
    *,
    status_override: str | None = None,
) -> None:
    try:
        await GatewayPersistenceService(session).stage_run(
            result,
            application_api_key_id=principal.key_id,
            config_version=config_version,
            started_at_override=operation.started_at,
            finished_at_override=response.finished_at,
            status_override=status_override,
        )
        body = error_body if error_body is not None else response.model_dump(mode="json")
        finalized = await operation_service.finalize_in_transaction(
            operation.request_id,
            status=response.status,
            public_result=response.model_dump(mode="json"),
            http_status=http_status,
            http_body=body,
            now=response.finished_at,
        )
        if not finalized:
            await session.rollback()
            raise RuntimeStorageError("Runtime operation finalization lost its state claim")
        await session.commit()
        try:
            await BudgetAlertService(session).notify_if_needed(result.request.application_id)
        except Exception:
            # Email delivery must never turn a completed gateway request into
            # a persistence failure. The delivery record retains the failure
            # for a later retry on the next usage event.
            logger.exception("Budget alert notification failed for %s", result.request.application_id)
    except (SQLAlchemyError, RuntimeStorageError) as exc:
        await session.rollback()
        raise RuntimeStorageError("Runtime request persistence failed") from exc
    except Exception as exc:
        await session.rollback()
        raise RuntimeStorageError("Runtime request persistence failed") from exc
