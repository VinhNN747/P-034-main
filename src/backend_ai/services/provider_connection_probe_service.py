from __future__ import annotations

import asyncio
import base64
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.gateway.multimodal.ocr import GeminiOcrClient
from src.backend_ai.gateway.providers.anthropic import AnthropicProvider
from src.backend_ai.gateway.providers.base import GenerationOptions
from src.backend_ai.gateway.providers.embeddings import OpenAIEmbeddingProvider
from src.backend_ai.gateway.providers.gemini import GeminiProvider
from src.backend_ai.gateway.providers.openai import OpenAIProvider
from src.backend_ai.gateway.providers.openrouter import OpenRouterProvider
from src.backend_ai.gateway.understanding.jev import TypeSafeJevClient
from src.shared.config import Settings
from src.shared.dtos.gateway import AttachmentKind
from src.shared.dtos.provider_connection_probe import ConnectionProbeInput, ConnectionProbeRead
from src.shared.models.core import utc_now
from src.shared.models.gateway import Model
from src.shared.repositories.audit import AuditRepository
from src.shared.repositories.provider_credentials import ProviderCredentialRepository
from src.shared.services.provider_credential_resolver import (
    CredentialUnavailableError,
    CredentialVersionMismatchError,
    ProviderCredentialResolver,
)

_PROBE_TIMEOUT_SECONDS = 10.0
_PROBE_PROMPT = "Classify this short connection check."
_OCR_SAMPLE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/F7sAAAAASUVORK5CYII="
)


class ProviderProbeNotFoundError(Exception):
    pass


class ProviderProbeConflictError(Exception):
    pass


class ProviderProbeInvalidError(Exception):
    pass


class ProviderConnectionProbeService:
    def __init__(
        self,
        session: AsyncSession,
        resolver: ProviderCredentialResolver,
        settings: Settings,
        *,
        actor_user_id: str | None = None,
    ) -> None:
        self.session = session
        self.resolver = resolver
        self.settings = settings
        self.actor_user_id = actor_user_id
        self.repository = ProviderCredentialRepository(session)
        self.audit = AuditRepository(session)

    async def probe(self, provider_id: str, payload: ConnectionProbeInput) -> ConnectionProbeRead:
        provider, credential = await self.repository.provider_with_credential(provider_id)
        if provider is None:
            raise ProviderProbeNotFoundError("Provider connection not found.")

        if provider.kind in {"offline", "local"}:
            if credential is not None or payload.expected_version != 0:
                raise ProviderProbeInvalidError("Offline connections do not have a credential to test.")
            result = self._read(
                provider_id,
                credential_version=None,
                payload=payload,
                status="skipped",
                error_code="SKIPPED_OFFLINE",
                error_message="This connection does not use an upstream credential.",
            )
            await self._audit_result(provider_id, result)
            if self.session.in_transaction():
                await self.session.commit()
            return result

        if self.settings.gateway_execution_mode == "offline":
            version = credential.version if credential is not None else None
            if (credential is None and payload.expected_version != 0) or (
                credential is not None and payload.expected_version != credential.version
            ):
                raise ProviderProbeConflictError("Credential version changed; reload and retry.")
            result = self._read(
                provider_id,
                credential_version=version,
                payload=payload,
                status="skipped",
                error_code="SKIPPED_OFFLINE",
                error_message="Provider tests are disabled while gateway execution is offline.",
            )
            if credential is not None and credential.status == "active" and credential.encrypted_api_key:
                if not await self.repository.record_probe_if_current(provider_id, credential.version, result):
                    await self.session.rollback()
                    raise ProviderProbeConflictError("Credential changed while the connection was being tested.")
            await self._audit_result(provider_id, result, credential_version=version)
            if self.session.in_transaction():
                await self.session.commit()
            return result

        self._validate_mode(provider.kind, provider.purpose, payload)
        payload = payload.model_copy(update={"model_name": self._effective_model_name(provider.kind, payload)})
        model_profile = None
        if provider.kind == "anthropic":
            model_profile = await self.session.scalar(select(Model).where(
                Model.provider_id == provider.id, Model.name == payload.model_name))
            if model_profile is None:
                raise ProviderProbeInvalidError("Anthropic chat probes require a model profile in the catalog.")

        if payload.expected_version < 1:
            raise ProviderProbeInvalidError("A saved active credential is required to test this connection.")
        if credential is None or credential.status != "active" or not credential.encrypted_api_key:
            result = self._read(
                provider_id,
                credential_version=credential.version if credential else None,
                payload=payload,
                status="unavailable",
                error_code="CREDENTIAL_UNAVAILABLE",
                error_message="No active credential is available for this connection.",
            )
            await self._audit_result(provider_id, result)
            if self.session.in_transaction():
                await self.session.commit()
            return result
        if credential.version != payload.expected_version:
            raise ProviderProbeConflictError("Credential version changed; reload and retry.")

        try:
            resolved = await self.resolver.resolve_provider_for_probe(
                provider_id,
                expected_version=payload.expected_version,
            )
        except CredentialVersionMismatchError as exc:
            raise ProviderProbeConflictError("Credential version changed; reload and retry.") from exc
        except CredentialUnavailableError:
            result = self._read(
                provider_id,
                credential_version=credential.version,
                payload=payload,
                status="unavailable",
                error_code="CREDENTIAL_UNAVAILABLE",
                error_message="The saved credential cannot be used for this connection.",
            )
            if not await self.repository.record_probe_if_current(provider_id, credential.version, result):
                await self.session.rollback()
                raise ProviderProbeConflictError("Credential changed while the connection was being tested.")
            await self._audit_result(provider_id, result, credential_version=credential.version)
            if self.session.in_transaction():
                await self.session.commit()
            return result

        # The key and metadata are now copied into a request-local immutable
        # value. Close the read transaction before making a network request.
        if self.session.in_transaction():
            await self.session.commit()

        started = perf_counter()
        try:
            async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
                if provider.kind == "anthropic":
                    await self._call_upstream(provider.kind, provider.base_url, resolved.api_key, payload,
                        model_profile=model_profile)
                else:
                    await self._call_upstream(provider.kind, provider.base_url, resolved.api_key, payload)
            elapsed_ms = round((perf_counter() - started) * 1000, 3)
            result = self._read(
                provider_id,
                credential_version=resolved.version,
                payload=payload,
                status="healthy",
                latency_ms=elapsed_ms,
            )
        except Exception as exc:  # upstream SDK errors may include response bodies; expose only fixed codes.
            elapsed_ms = round((perf_counter() - started) * 1000, 3)
            code, message = _sanitize_provider_error(exc)
            result = self._read(
                provider_id,
                credential_version=resolved.version,
                payload=payload,
                status="failed",
                error_code=code,
                error_message=message,
                latency_ms=elapsed_ms,
            )

        if not await self.repository.record_probe_if_current(provider_id, resolved.version, result):
            await self.session.rollback()
            raise ProviderProbeConflictError("Credential changed while the connection was being tested.")
        await self._audit_result(provider_id, result, credential_version=resolved.version)
        await self.session.commit()
        return result

    async def _audit_result(
        self,
        provider_id: str,
        result: ConnectionProbeRead,
        *,
        credential_version: int | None = None,
    ) -> None:
        await self.audit.add_event(
            actor_user_id=self.actor_user_id,
            action="provider_connection.probed",
            resource_type="provider_connection",
            resource_id=provider_id,
            metadata={
                "credential_version": credential_version,
                "mode": result.mode,
                "model_name": result.model_name,
                "status": result.status,
                "error_code": result.error_code,
                "latency_ms": result.latency_ms,
            },
        )

    def _validate_mode(self, kind: str, purpose: str, payload: ConnectionProbeInput) -> None:
        allowed = {
            "chat": {"openai", "openrouter", "google", "anthropic"},
            "embedding": {"openai"},
            "ocr": {"google"},
            "jev": {"typesafe"},
        }
        if kind not in allowed[payload.mode]:
            raise ProviderProbeInvalidError("Probe mode is not supported for this provider kind.")
        if kind == "anthropic" and purpose != "target":
            raise ProviderProbeInvalidError("Anthropic probes require a target connection.")
        if payload.mode == "jev" and purpose != "internal":
            raise ProviderProbeInvalidError("JEV probes require an internal TypeSafe connection.")

    def _effective_model_name(self, kind: str, payload: ConnectionProbeInput) -> str:
        if payload.mode == "jev":
            return self.settings.gateway_jev_model
        if payload.model_name:
            return payload.model_name.strip()
        if payload.mode == "embedding":
            return self.settings.support_chat_embedding_model
        if payload.mode == "ocr":
            return self.settings.gateway_ocr_model
        return self._default_chat_model(kind)

    async def _call_upstream(self, kind: str, base_url: str | None, api_key: str, payload: ConnectionProbeInput,
                             *, model_profile: Model | None = None) -> None:
        model = payload.model_name
        if payload.mode == "jev":
            client = TypeSafeJevClient(
                api_key=api_key,
                model=self.settings.gateway_jev_model,
                timeout_seconds=_PROBE_TIMEOUT_SECONDS,
            )
            await client.decide(_PROBE_PROMPT)
            return
        if payload.mode == "embedding":
            await OpenAIEmbeddingProvider(api_key=api_key, base_url=base_url).probe(
                model or self.settings.support_chat_embedding_model,
                timeout_seconds=_PROBE_TIMEOUT_SECONDS,
                input_text="connection test",
            )
            return
        if payload.mode == "ocr":
            client = GeminiOcrClient(
                api_key,
                model=model or self.settings.gateway_ocr_model,
                timeout_seconds=_PROBE_TIMEOUT_SECONDS,
                max_output_tokens=64,
            )
            await client.ocr(_OCR_SAMPLE, "image/png", kind=AttachmentKind.IMAGE)
            return

        options = GenerationOptions(max_output_tokens=64, hard_completion_token_limit=64,
            low_reasoning=kind == "anthropic")
        if kind == "openai":
            client = OpenAIProvider(enabled=True, api_key=api_key, base_url=base_url)
        elif kind == "openrouter":
            client = OpenRouterProvider(api_key=api_key, base_url=base_url)
        elif kind == "google":
            client = GeminiProvider(api_key=api_key)
        elif kind == "anthropic":
            if model_profile is None:
                raise ProviderProbeInvalidError("Anthropic chat probes require a model profile in the catalog.")
            client = AnthropicProvider(api_key=api_key, base_url=base_url,
                model_profiles={model_profile.name: model_profile})
        else:
            raise ProviderProbeInvalidError("Probe mode is not supported for this provider kind.")
        response = await client.generate(
            "Reply OK.",
            task_id="provider_connection_probe",
            model_id=model or self._default_chat_model(kind),
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
            options=options,
        )
        if kind == "anthropic" and not response.text.strip():
            raise RuntimeError("Anthropic connection probe returned no visible output.")

    def _default_chat_model(self, kind: str) -> str:
        return {
            "openai": self.settings.model_name,
            "openrouter": self.settings.gateway_understander_model,
            "google": self.settings.gateway_gemini_model,
            "anthropic": self.settings.gateway_anthropic_probe_model,
        }[kind]

    @staticmethod
    def _read(
        provider_id: str,
        *,
        credential_version: int | None,
        payload: ConnectionProbeInput,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
        latency_ms: float = 0.0,
    ) -> ConnectionProbeRead:
        return ConnectionProbeRead(
            provider_id=provider_id,
            credential_version=credential_version,
            status=status,
            mode=payload.mode,
            model_name=payload.model_name,
            checked_at=utc_now(),
            latency_ms=latency_ms,
            error_code=error_code,
            error_message=error_message,
        )


def _sanitize_provider_error(exc: Exception) -> tuple[str, str]:
    chain: list[Exception] = []
    seen: set[int] = set()
    current: Exception | None = exc
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, Exception) else None

    codes = {
        str(getattr(item, "code", "")).lower()
        for item in chain
        if getattr(item, "code", None) is not None
    }
    for item in chain:
        body = getattr(item, "body", None)
        error_body = body.get("error") if isinstance(body, dict) else None
        if isinstance(error_body, dict) and isinstance(error_body.get("code"), str):
            codes.add(error_body["code"].lower())
    if codes & {"insufficient_quota", "quota_exceeded", "billing_hard_limit_reached"}:
        return "QUOTA_EXCEEDED", "The provider account has no available quota."
    if codes & {"model_not_found", "model_not_available"}:
        return "MODEL_UNAVAILABLE", "The provider could not find this model."
    if codes & {"embedding_timeout", "timed_out"}:
        return "TIMEOUT", "The provider test exceeded the 10 second timeout."
    if codes & {"embedding_network_error", "connection_error"}:
        return "NETWORK_ERROR", "The provider could not be reached."
    status = next(
        (getattr(item, "status_code", None) for item in chain if getattr(item, "status_code", None) is not None),
        None,
    )
    if status == 401:
        return "AUTHENTICATION_FAILED", "The provider rejected this credential."
    if status == 403:
        return "ACCESS_DENIED", "The provider denied access to this model or action."
    if status == 402:
        return "QUOTA_EXCEEDED", "The provider account has no available quota."
    if status == 429:
        return "RATE_LIMITED", "The provider rate limit was reached."
    if status == 404:
        return "MODEL_UNAVAILABLE", "The provider could not find this model."
    if status == 400:
        return "PROVIDER_ERROR", "The provider rejected the test request."
    if any(
        isinstance(item, (TimeoutError, asyncio.TimeoutError)) or "timeout" in type(item).__name__.lower()
        for item in chain
    ):
        return "TIMEOUT", "The provider test exceeded the 10 second timeout."
    if any("connection" in type(item).__name__.lower() or "network" in type(item).__name__.lower() for item in chain):
        return "NETWORK_ERROR", "The provider could not be reached."
    return "PROVIDER_ERROR", "The provider test failed."
