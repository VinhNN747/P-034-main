from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.dtos.openai_embeddings import EmbeddingRequest
from src.backend_ai.gateway.providers.embeddings import (
    EmbeddingProviderError,
    EmbeddingProviderResult,
    OpenAIEmbeddingProvider,
    serialize_embeddings,
)
from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.backend_ai.routers.runtime import _runtime_headers
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.backend_ai.services.embedding_persistence import EmbeddingPersistence
from src.backend_ai.services.embedding_validation import (
    EmbeddingModelProfile,
    ValidatedEmbeddingInput,
    resolve_embedding_model,
    validate_embedding_input,
)
from src.backend_ai.services.openai_operation_fingerprint import build_openai_fingerprint
from src.backend_ai.services.runtime_operation_service import (
    InvalidIdempotencyKeyError,
    RuntimeOperationService,
    RuntimeStorageError,
)
from src.shared.config import Settings, get_settings
from src.shared.dtos.runtime_api import (
    RuntimeAPIError,
    RuntimeCost,
    RuntimeOperationError,
    RuntimeRequestResponse,
    RuntimeUsage,
    RuntimeUsageScope,
)
from src.shared.models.gateway import ApplicationGatewayConfig, Model, Provider
from src.shared.models.runtime import RuntimeOperationRecord
from src.shared.services.credential_crypto import CredentialConfigurationError, ensure_credential_crypto
from src.shared.services.provider_credential_resolver import (
    CredentialUnavailableError,
    ProviderCredentialResolver,
)


@dataclass(frozen=True)
class EmbeddingModelSnapshot:
    id: str
    name: str
    input_price_per_million: float
    pricing_version: str


@dataclass(frozen=True)
class EmbeddingProviderSnapshot:
    id: str
    name: str
    kind: str
    base_url: str | None


@dataclass(frozen=True)
class EmbeddingConfigSnapshot:
    version: int
    retry_limit: int
    timeout_seconds: float


@dataclass(frozen=True)
class EmbeddingOperationSnapshot:
    request_id: str
    application_id: str
    config_version: int
    started_at: datetime


class EmbeddingExecutionService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    async def execute(
        self,
        request: Request,
        principal: ApplicationPrincipal,
        payload: EmbeddingRequest,
        *,
        idempotency_key: str,
        request_id: str,
    ) -> JSONResponse:
        try:
            RuntimeOperationService.validate_idempotency_key(idempotency_key)
        except InvalidIdempotencyKeyError as exc:
            raise OpenAIAPIError(
                400, "Idempotency-Key must contain 1 to 128 visible ASCII characters.", code="invalid_idempotency_key"
            ) from exc

        generic = EmbeddingModelProfile(payload.model, native_dimensions=3072)
        validate_embedding_input(payload, generic)
        fingerprint = build_openai_fingerprint("openai_embeddings", _fingerprint_payload(payload))
        operation_service = RuntimeOperationService(
            self.session,
            requests_per_minute=self.settings.gateway_runtime_requests_per_minute,
            max_concurrent_requests=self.settings.gateway_runtime_max_concurrent_requests,
            deadline_seconds=int(self.settings.gateway_runtime_deadline_seconds),
        )
        try:
            existing = await operation_service.lookup_normalized(principal, idempotency_key, fingerprint)
        except (RuntimeStorageError, SQLAlchemyError) as exc:
            raise RuntimeAPIError(
                503,
                "RUNTIME_STORAGE_UNAVAILABLE",
                "Runtime operation could not be loaded.",
                retryable=True,
                request_id=request_id,
            ) from exc
        if existing is not None:
            return _post_response(existing, idempotency_key, request_id)

        if not self.settings.gateway_runtime_enabled or self.settings.gateway_execution_mode != "live":
            raise OpenAIAPIError(
                503,
                "The live embedding runtime is not enabled.",
                type="server_error",
                code="embedding_provider_unavailable",
            )
        if getattr(principal, "pipeline", "online") == "offline":
            # Embeddings run on external providers; an offline pipeline calls none.
            raise OpenAIAPIError(
                503,
                "Embeddings are not available for offline pipelines.",
                type="server_error",
                code="embedding_provider_unavailable",
            )
        try:
            model, provider, profile = await resolve_embedding_model(self.session, principal, payload.model)
            validated = validate_embedding_input(payload, profile)
            if validated.input_count > self.settings.gateway_embeddings_max_batch_size:
                raise OpenAIAPIError(
                    400, "input exceeds the gateway batch limit.", code="invalid_request", param="input"
                )
            config = await self.session.scalar(
                select(ApplicationGatewayConfig).where(
                    ApplicationGatewayConfig.application_id == principal.application_id
                )
            )
        except SQLAlchemyError as exc:
            raise RuntimeAPIError(
                503,
                "MODEL_CATALOG_UNAVAILABLE",
                "The embedding model could not be loaded.",
                retryable=True,
                request_id=request_id,
            ) from exc
        if config is None:
            raise OpenAIAPIError(
                503,
                "The application gateway is not configured.",
                type="server_error",
                code="embedding_configuration_unavailable",
            )
        if not self.settings.gateway_openai_enabled:
            raise OpenAIAPIError(
                503,
                "The embedding provider is not configured.",
                type="server_error",
                code="embedding_provider_unavailable",
            )
        try:
            crypto = ensure_credential_crypto(self.settings)
            credential = await ProviderCredentialResolver(self.session, crypto).resolve_provider(provider.id)
        except (CredentialConfigurationError, CredentialUnavailableError) as exc:
            raise OpenAIAPIError(
                503,
                "The embedding provider is not configured.",
                type="server_error",
                code="embedding_provider_unavailable",
            ) from exc

        # Claim begins a fresh write transaction. Its read rollback expires ORM
        # instances, so all execution inputs must be captured before that boundary.
        model = EmbeddingModelSnapshot(model.id, model.name, model.input_price_per_million, model.pricing_version)
        provider = EmbeddingProviderSnapshot(provider.id, provider.name, provider.kind, provider.base_url)
        config = EmbeddingConfigSnapshot(config.version, config.retry_limit, config.timeout_seconds)
        snapshot = _config_snapshot(model, provider, config, profile, self.settings)
        try:
            claim = await operation_service.claim_normalized(
                principal,
                idempotency_key,
                fingerprint,
                config_version=config.version,
                config_snapshot=snapshot,
                execution_mode="live",
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
            return _post_response(claim, idempotency_key, request_id)
        operation = claim.operation
        if operation is None:
            raise RuntimeAPIError(
                503,
                "RUNTIME_STORAGE_UNAVAILABLE",
                "Runtime claim returned no operation.",
                retryable=True,
                request_id=request_id,
            )

        operation = EmbeddingOperationSnapshot(
            operation.request_id, operation.application_id, operation.config_version, operation.started_at
        )
        request.state.request_id = operation.request_id
        request.state.operation_id = operation.request_id
        return await self._execute_claimed(
            request,
            principal,
            payload,
            validated,
            model,
            provider,
            profile,
            config,
            operation,
            operation_service,
            idempotency_key,
            credential.api_key,
        )

    async def _execute_claimed(
        self,
        request: Request,
        principal: ApplicationPrincipal,
        payload: EmbeddingRequest,
        validated: ValidatedEmbeddingInput,
        model: Model,
        provider: Provider,
        profile: EmbeddingModelProfile,
        config: ApplicationGatewayConfig,
        operation: RuntimeOperationRecord,
        operation_service: RuntimeOperationService,
        idempotency_key: str,
        api_key: str,
    ) -> JSONResponse:
        started = operation.started_at
        deadline = time.monotonic() + self.settings.gateway_runtime_deadline_seconds
        client = OpenAIEmbeddingProvider(api_key=api_key, base_url=provider.base_url)
        result: EmbeddingProviderResult | None = None
        provider_error: EmbeddingProviderError | None = None
        attempts = 0
        max_retries = max(0, config.retry_limit)
        while attempts <= max_retries:
            attempts += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return await self._unknown_response(
                    request,
                    principal,
                    payload,
                    validated,
                    model,
                    provider,
                    profile,
                    config,
                    operation,
                    operation_service,
                    idempotency_key,
                )
            timeout = min(float(config.timeout_seconds), remaining)
            try:
                result = await asyncio.wait_for(
                    client.embed(payload, validated, timeout_seconds=timeout), timeout=remaining
                )
                provider_error = None
                break
            except TimeoutError:
                return await self._unknown_response(
                    request,
                    principal,
                    payload,
                    validated,
                    model,
                    provider,
                    profile,
                    config,
                    operation,
                    operation_service,
                    idempotency_key,
                )
            except EmbeddingProviderError as exc:
                provider_error = exc
                if exc.outcome_unknown:
                    return await self._unknown_response(
                        request,
                        principal,
                        payload,
                        validated,
                        model,
                        provider,
                        profile,
                        config,
                        operation,
                        operation_service,
                        idempotency_key,
                    )
                if not exc.retryable or attempts > max_retries:
                    break
                remaining = deadline - time.monotonic()
                delay = min(max(0.0, exc.retry_after or 0.05 * attempts), remaining)
                if delay <= 0:
                    break
                await asyncio.sleep(delay)

        finished = datetime.now(UTC)
        if result is None:
            return await self._finalize_known_failure(
                request,
                principal,
                payload,
                validated,
                model,
                provider,
                profile,
                config,
                operation,
                operation_service,
                idempotency_key,
                provider_error,
                started,
                finished,
            )

        public = serialize_embeddings(result, payload.encoding_format)
        content = public.model_dump(mode="json")
        encoded = json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        too_large = len(encoded) > self.settings.gateway_embeddings_max_result_bytes
        if too_large:
            return await self._finalize_result_too_large(
                request,
                principal,
                payload,
                validated,
                model,
                provider,
                profile,
                config,
                operation,
                operation_service,
                idempotency_key,
                result,
                started,
                finished,
            )
        receipt = _receipt(
            operation, self.settings, model, provider, result, started, finished, status="completed", error=None
        )
        return await self._persist_and_respond(
            request,
            principal,
            payload,
            validated,
            model,
            provider,
            profile,
            config,
            operation,
            operation_service,
            idempotency_key,
            result,
            receipt,
            content,
            200,
            "completed",
            started,
            finished,
        )

    async def _finalize_known_failure(
        self,
        request,
        principal,
        payload,
        validated,
        model,
        provider,
        profile,
        config,
        operation,
        operation_service,
        idempotency_key,
        provider_error,
        started,
        finished,
    ):
        error = provider_error or EmbeddingProviderError("The embedding provider request failed.")
        status_code, public_code, message, retry_after = _public_provider_error(error)
        receipt = _receipt(
            operation,
            self.settings,
            model,
            provider,
            None,
            started,
            finished,
            status="failed",
            error=(public_code, message),
        )
        body = _runtime_error_body(public_code, message, operation.request_id, retry_after=retry_after)
        return await self._persist_and_respond(
            request,
            principal,
            payload,
            validated,
            model,
            provider,
            profile,
            config,
            operation,
            operation_service,
            idempotency_key,
            None,
            receipt,
            body,
            status_code,
            "failed",
            started,
            finished,
            error_code=public_code,
        )

    async def _finalize_result_too_large(
        self,
        request,
        principal,
        payload,
        validated,
        model,
        provider,
        profile,
        config,
        operation,
        operation_service,
        idempotency_key,
        result,
        started,
        finished,
    ):
        message = "The embedding result exceeds the gateway response size limit."
        code = "embedding_result_too_large"
        receipt = _receipt(
            operation, self.settings, model, provider, result, started, finished, status="failed", error=(code, message)
        )
        body = _runtime_error_body(code, message, operation.request_id)
        return await self._persist_and_respond(
            request,
            principal,
            payload,
            validated,
            model,
            provider,
            profile,
            config,
            operation,
            operation_service,
            idempotency_key,
            result,
            receipt,
            body,
            502,
            "failed",
            started,
            finished,
            error_code=code,
        )

    async def _unknown_response(
        self,
        request,
        principal,
        payload,
        validated,
        model,
        provider,
        profile,
        config,
        operation,
        operation_service,
        idempotency_key,
    ):
        now = datetime.now(UTC)
        code = "operation_outcome_unknown"
        message = "The embedding request may have reached the provider; inspect its receipt before retrying."
        receipt = _receipt(
            operation,
            self.settings,
            model,
            provider,
            None,
            operation.started_at,
            now,
            status="indeterminate",
            error=(code, message),
        )
        body = _runtime_error_body(code, message, operation.request_id)
        return await self._persist_and_respond(
            request,
            principal,
            payload,
            validated,
            model,
            provider,
            profile,
            config,
            operation,
            operation_service,
            idempotency_key,
            None,
            receipt,
            body,
            503,
            "indeterminate",
            operation.started_at,
            now,
            error_code=code,
        )

    async def _persist_and_respond(
        self,
        request,
        principal,
        payload,
        validated,
        model,
        provider,
        profile,
        config,
        operation,
        operation_service,
        idempotency_key,
        result,
        receipt,
        http_body,
        http_status,
        operation_status,
        started,
        finished,
        error_code=None,
    ):
        try:
            await EmbeddingPersistence(self.session).stage_run(
                principal,
                operation.request_id,
                model.id,
                provider.name,
                result,
                receipt,
                input_count=validated.input_count,
                dimensions=validated.expected_dimensions,
                input_price_per_million=Decimal(str(model.input_price_per_million)),
                config_version=config.version,
                started_at=started,
                finished_at=finished,
                error_code=error_code,
            )
            finalized = await operation_service.finalize_in_transaction(
                operation.request_id,
                status=operation_status,
                public_result=receipt.model_dump(mode="json"),
                http_status=http_status,
                http_body=http_body,
                now=finished,
            )
            if not finalized:
                raise RuntimeStorageError("Embedding operation lost its state claim")
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            return await self._mark_unknown_after_commit_failure(request, operation_service, operation, idempotency_key)

        headers = _runtime_headers(operation.request_id, idempotency_key, replayed=False)
        request.state.request_id = operation.request_id
        if http_status == 200:
            return JSONResponse(status_code=200, content=http_body, headers=headers)
        error_body = http_body.get("error") if isinstance(http_body, dict) else None
        details = error_body.get("details") if isinstance(error_body, dict) else None
        retry_after = details.get("retry_after") if isinstance(details, dict) else None
        if error_code and retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return JSONResponse(status_code=http_status, content=http_body, headers=headers)

    async def _mark_unknown_after_commit_failure(self, request, operation_service, operation, idempotency_key):
        now = datetime.now(UTC)
        message = "The request ran but its final receipt could not be confirmed."
        body = _runtime_error_body("operation_outcome_unknown", message, operation.request_id)
        receipt = _receipt(
            operation,
            self.settings,
            None,
            None,
            None,
            operation.started_at,
            now,
            status="indeterminate",
            error=("operation_outcome_unknown", message),
        )
        try:
            await operation_service.finalize(
                operation.request_id,
                status="indeterminate",
                public_result=receipt.model_dump(mode="json"),
                http_status=503,
                http_body=body,
                now=now,
            )
        except Exception:
            await self.session.rollback()
        request.state.request_id = operation.request_id
        return JSONResponse(
            status_code=503,
            content=body,
            headers=_runtime_headers(operation.request_id, idempotency_key, replayed=False),
        )


def _fingerprint_payload(payload: EmbeddingRequest) -> dict[str, Any]:
    return {
        "model": payload.model,
        "input": payload.input,
        "encoding_format": payload.encoding_format,
        "dimensions": payload.dimensions,
        "user": payload.user,
    }


def _config_snapshot(model, provider, config, profile, settings) -> dict[str, Any]:
    return {
        "execution_mode": "live",
        "config_version": config.version,
        "model": {
            "id": model.id,
            "name": model.name,
            "provider_id": provider.id,
            "provider_name": provider.name,
            "provider_kind": provider.kind,
            "input_price_per_million": model.input_price_per_million,
            "pricing_version": model.pricing_version,
            "dimensions": profile.native_dimensions,
        },
        "limits": {
            "max_batch_size": settings.gateway_embeddings_max_batch_size,
            "max_body_bytes": settings.gateway_embeddings_max_body_bytes,
            "max_result_bytes": settings.gateway_embeddings_max_result_bytes,
            "max_input_tokens": profile.max_input_tokens,
            "max_total_tokens": profile.max_total_tokens,
        },
    }


def _receipt(operation, settings, model, provider, result, started, finished, *, status, error):
    input_tokens = result.input_tokens if result else None
    usage = None
    if input_tokens is not None:
        scope = RuntimeUsageScope(
            input_tokens=input_tokens,
            output_tokens=0,
            total_tokens=input_tokens,
        )
        usage = RuntimeUsage(execution=scope, pipeline=scope, complete=True)
    cost = None
    if result and model:
        amount = Decimal(result.input_tokens) * Decimal(str(model.input_price_per_million)) / Decimal(1_000_000)
        cost = RuntimeCost(estimated_cost=float(amount), estimated=True)
    operation_error = (
        RuntimeOperationError(code=error[0], message=error[1], retryable=False, details={}) if error else None
    )
    return RuntimeRequestResponse(
        request_id=operation.request_id,
        application_id=operation.application_id,
        status=status,
        execution_mode="live",
        output=None,
        models_used=[model.id] if model else [],
        providers_used=[provider.name] if provider else [],
        usage=usage,
        cost=cost,
        latency_ms=result.latency_ms if result else None,
        config_version=operation.config_version,
        started_at=started,
        finished_at=finished,
        error=operation_error,
    )


def _runtime_error_body(code: str, message: str, request_id: str, *, retry_after: float | None = None):
    details = {"retry_after": retry_after} if retry_after is not None else {}
    return {
        "error": {"code": code, "message": message, "retryable": False, "details": details},
        "request_id": request_id,
    }


def _public_provider_error(error: EmbeddingProviderError):
    if error.code == "invalid_provider_response":
        return 502, error.code, "The embedding provider returned an invalid response.", None
    if error.status_code == 429:
        return 429, "embedding_provider_rate_limited", "The embedding provider is rate limited.", error.retry_after
    if error.status_code == 401 or error.status_code == 403:
        return 502, "embedding_provider_unavailable", "The embedding provider is not configured correctly.", None
    retry_after = error.retry_after if error.retryable else None
    return 502, "embedding_provider_unavailable", "The embedding provider request failed.", retry_after


def _post_response(claim, idempotency_key: str, request_id: str):
    operation = claim.operation
    operation_id = operation.request_id if operation else request_id
    if claim.kind == "conflict":
        raise RuntimeAPIError(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key was already used with a different request body.",
            request_id=operation_id,
        )
    if claim.kind in {"running", "owner"}:
        raise RuntimeAPIError(
            409,
            "REQUEST_IN_PROGRESS",
            "Request is still running.",
            retryable=True,
            details={"request_id": operation_id},
            headers={"Retry-After": str(claim.retry_after or 1)},
            request_id=operation_id,
        )
    if claim.kind in {"unknown", "indeterminate"}:
        raise RuntimeAPIError(
            409, "REQUEST_OUTCOME_UNKNOWN", "The request outcome could not be confirmed.", request_id=operation_id
        )
    if claim.kind in {"replay", "terminal_replay"} and operation:
        if operation.http_body_json is None:
            raise RuntimeAPIError(
                502, "INVALID_RUNTIME_RESPONSE", "The stored embedding result is unavailable.", request_id=operation_id
            )
        headers = _runtime_headers(operation_id, idempotency_key, replayed=True)
        details = (
            operation.http_body_json.get("error", {}).get("details", {})
            if isinstance(operation.http_body_json, dict)
            else {}
        )
        if details.get("retry_after") is not None:
            headers["Retry-After"] = str(details["retry_after"])
        return JSONResponse(
            status_code=operation.http_status or (200 if claim.kind == "replay" else 502),
            content=operation.http_body_json,
            headers=headers,
        )
    if claim.kind in {"rate_limited", "concurrency_limited"}:
        raise RuntimeAPIError(
            429,
            "RATE_LIMIT_EXCEEDED",
            "Application request limit exceeded.",
            retryable=True,
            headers={"Retry-After": str(claim.retry_after or 1)},
            request_id=request_id,
        )
    raise RuntimeAPIError(
        503,
        "RUNTIME_STORAGE_UNAVAILABLE",
        "Operation claim could not be resolved.",
        retryable=True,
        request_id=request_id,
    )
