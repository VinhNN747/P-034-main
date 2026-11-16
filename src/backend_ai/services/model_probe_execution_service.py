from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.gateway.providers.base import GenerationOptions
from src.backend_ai.gateway.providers.embeddings import OpenAIEmbeddingProvider
from src.backend_ai.services.provider_client_builder import ProviderClientBuilder
from src.shared.config import Settings
from src.shared.dtos.model_catalog import ModelProbeRead
from src.shared.models.gateway import Model, Provider
from src.shared.services.credential_crypto import CredentialConfigurationError, ensure_credential_crypto
from src.shared.services.local_endpoints import is_local_endpoint
from src.shared.services.provider_credential_resolver import (
    CredentialUnavailableError,
    ProviderCredentialResolver,
)


class ModelProbeExecutionService:
    def __init__(self, session: AsyncSession, settings: Settings):
        self.session = session
        self.settings = settings

    async def probe(self, model_id: str) -> ModelProbeRead:
        started = perf_counter()
        model = await self.session.get(Model, model_id)
        if model is None:
            raise LookupError("Model not found")
        provider = await self.session.get(Provider, model.provider_id)
        if provider is None:
            raise LookupError("Model provider not found")
        resolved_model_id = model.id
        resolved_model_name = model.name
        provider_id = provider.id
        base_url = provider.base_url

        credential_version = None
        status = "unavailable"
        reason = "The selected provider connection is unavailable."
        if self.settings.gateway_execution_mode == "offline" and not is_local_endpoint(provider):
            status = "offline"
            reason = "Offline mode: provider call was not attempted."
        elif provider.status != "active" or provider.purpose != "target":
            reason = "The model provider is not an active target connection."
        else:
            try:
                if is_local_endpoint(provider):
                    # A self-hosted server's key is optional, so a missing encryption key only
                    # means it is called without one.
                    try:
                        resolver = ProviderCredentialResolver(self.session, ensure_credential_crypto(self.settings))
                    except CredentialConfigurationError:
                        resolver = None
                else:
                    resolver = ProviderCredentialResolver(self.session, ensure_credential_crypto(self.settings))
                capabilities = {str(value).strip().lower() for value in (model.capabilities_json or [])}
                embedding_only = "embedding" in capabilities and not capabilities.intersection(
                    {"chat", "text", "completion", "generation"}
                )
                timeout = max(0.1, self.settings.gateway_routing_timeout_seconds)
                if embedding_only:
                    if provider.kind != "openai" or not self.settings.gateway_openai_enabled:
                        reason = "The embedding adapter for this connection is unavailable."
                    else:
                        credential = await resolver.resolve_provider(provider.id)
                        adapter = OpenAIEmbeddingProvider(
                            api_key=credential.api_key,
                            base_url=base_url,
                        )
                        if self.session.in_transaction():
                            await self.session.commit()
                        await asyncio.wait_for(
                            adapter.probe(resolved_model_name, timeout_seconds=timeout), timeout=timeout
                        )
                        credential_version = credential.version
                        status = "healthy"
                        reason = None
                else:
                    builder = ProviderClientBuilder(resolver, self.settings)
                    if provider.kind == "anthropic":
                        adapter = await builder.build_for_provider(provider, model_profile=model)
                    else:
                        adapter = await builder.build_for_provider(provider)
                    if adapter is None:
                        reason = "The provider credential or adapter is unavailable."
                    else:
                        credential_version = builder.credential_versions.get(provider_id)
                        if self.session.in_transaction():
                            await self.session.commit()
                        anthropic_probe = provider.kind == "anthropic"
                        probe_budget = min(model.max_output_tokens or 64, 64) if anthropic_probe else 8
                        response = await asyncio.wait_for(
                            adapter.generate(
                                "Reply OK." if provider.kind in {"openai", "anthropic"} else "health probe",
                                task_id="catalog-probe",
                                model_id=resolved_model_name,
                                timeout_seconds=timeout,
                                options=GenerationOptions(max_output_tokens=probe_budget,
                                    hard_completion_token_limit=probe_budget if anthropic_probe else None,
                                    low_reasoning=anthropic_probe),
                            ),
                            timeout=timeout,
                        )
                        if provider.kind == "anthropic" and not response.text.strip():
                            raise RuntimeError("Anthropic model probe returned no visible output.")
                        status = "healthy"
                        reason = None
            except (CredentialConfigurationError, CredentialUnavailableError):
                reason = "The provider credential is unavailable."
            except TimeoutError:
                status = "unhealthy"
                reason = "The provider probe timed out."
            except Exception:
                status = "unhealthy"
                reason = "Provider probe failed; check the credential and model configuration."

        return ModelProbeRead(
            model_id=resolved_model_id,
            status=status,
            checked_at=datetime.now(UTC),
            latency_ms=round((perf_counter() - started) * 1000, 3),
            credential_version=credential_version,
            failure_reason=reason,
        )
