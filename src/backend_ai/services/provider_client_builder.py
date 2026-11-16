from collections import Counter
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from src.backend_ai.gateway.providers.anthropic import AnthropicProvider
from src.backend_ai.gateway.providers.base import ProviderClient
from src.backend_ai.gateway.providers.gemini import GeminiProvider
from src.backend_ai.gateway.providers.latency_observed import LatencyObservedProvider
from src.backend_ai.gateway.providers.local import LocalProvider
from src.backend_ai.gateway.providers.offline import OfflineProvider
from src.backend_ai.gateway.providers.openai import OpenAIProvider
from src.backend_ai.gateway.providers.openrouter import OpenRouterProvider
from src.backend_ai.latency.contracts import ModelKey, connection_fingerprint
from src.shared.config import Settings
from src.shared.models.gateway import Model, Provider
from src.shared.services.local_endpoints import is_local_endpoint
from src.shared.services.provider_credential_resolver import (
    CredentialUnavailableError,
    ProviderCredentialResolver,
)


class ProviderClientBuilder:
    """Resolve and construct one provider client per target connection per request."""

    def __init__(self, resolver: ProviderCredentialResolver, settings: Settings, latency_runtime=None):
        self.resolver = resolver
        self.settings = settings
        self.latency_runtime = latency_runtime
        self.available_provider_ids: set[str] = set()
        self.credential_versions: dict[str, int] = {}
        self.model_keys: dict[str, ModelKey] = {}
        self.model_bindings: dict[str, ModelKey] = {}

    async def build_for_models(
        self,
        rows: Sequence[tuple[Model, Provider]],
    ) -> dict[str, ProviderClient]:
        self.available_provider_ids = set()
        self.credential_versions = {}
        self.model_keys = {}
        self.model_bindings = {}
        provider_rows: dict[str, Provider] = {}
        profiles_by_provider: dict[str, dict[str, Any]] = {}
        for model, provider in rows:
            if model.status != "active" or provider.status != "active" or provider.purpose != "target":
                continue
            provider_rows[provider.id] = provider
            profiles = profiles_by_provider.setdefault(provider.id, {})
            profiles[model.name] = model
            profiles[model.id] = model

        clients_by_provider: dict[str, ProviderClient] = {}
        for provider_id, provider in provider_rows.items():
            client = await self.build_for_provider(provider, model_profiles=profiles_by_provider[provider_id])
            if client is not None:
                clients_by_provider[provider_id] = client
                self.available_provider_ids.add(provider_id)

        output: dict[str, ProviderClient] = {}
        names = Counter(model.name for model, provider in rows if provider.id in clients_by_provider)
        bindings_by_provider: dict[str, dict[str, ModelKey]] = {}
        names_by_provider: dict[str, Counter[str]] = {}
        for model, provider in rows:
            if provider.id not in clients_by_provider:
                continue
            key = ModelKey(
                catalog_model_id=model.id,
                provider_id=provider.id,
                upstream_model_name=model.name,
                connection_fingerprint=connection_fingerprint(
                    provider.kind,
                    provider.base_url,
                    self.credential_versions.get(provider.id),
                    self.settings.gateway_latency_region,
                ),
                provider_kind=provider.kind,
            )
            self.model_keys[model.id] = key
            bindings_by_provider.setdefault(provider.id, {})[model.id] = key
            names_by_provider.setdefault(provider.id, Counter())[model.name] += 1
            self.model_bindings[model.id] = key
            if names[model.name] == 1:
                self.model_bindings[model.name] = key
        if (
            self.latency_runtime is not None
            and self.latency_runtime.config.mode != "off"
            and self.settings.gateway_execution_mode == "live"
        ):
            collector = self.latency_runtime.collector
            for provider_id, bindings in bindings_by_provider.items():
                for model, provider in rows:
                    if provider.id == provider_id and names_by_provider[provider_id][model.name] == 1:
                        bindings[model.name] = self.model_keys[model.id]
                clients_by_provider[provider_id] = LatencyObservedProvider(
                    clients_by_provider[provider_id], bindings=bindings, collector=collector
                )
        for model, provider in rows:
            client = clients_by_provider.get(provider.id)
            if client is None:
                continue
            output[model.id] = client
            output[provider.name] = client
            if names[model.name] == 1:
                output[model.name] = client
        return output

    async def build_for_provider(self, provider: Provider, *, model_profile: Model | None = None,
                                 model_profiles: dict[str, Any] | None = None) -> ProviderClient | None:
        """Build a selected target connection, including for an inactive model probe."""
        if provider.status != "active" or provider.purpose != "target":
            return None
        profiles = dict(model_profiles or {})
        if model_profile is not None:
            profiles[model_profile.name] = model_profile
            profiles[model_profile.id] = model_profile
        if provider.kind == "anthropic":
            profiles = {key: _snapshot_anthropic_model_profile(profile) for key, profile in profiles.items()}
        return await self._build_provider(provider, model_profiles=profiles)

    async def _build_provider(self, provider: Provider, *, model_profiles: dict[str, Any] | None = None) -> ProviderClient | None:
        # A self-hosted server is not an external provider, so it runs in offline mode too.
        if is_local_endpoint(provider):
            return await self._build_local(provider)
        if self.settings.gateway_execution_mode == "offline":
            if provider.kind in {"offline", "local"}:
                return OfflineProvider()
            return None
        enabled = {
            "openai": self.settings.gateway_openai_enabled,
            "google": self.settings.gateway_gemini_enabled,
            "openrouter": self.settings.gateway_openrouter_enabled,
            "anthropic": self.settings.gateway_anthropic_enabled,
        }.get(provider.kind, False)
        if not enabled:
            return None
        try:
            credential = await self.resolver.resolve_provider(provider.id)
        except CredentialUnavailableError:
            return None
        self.credential_versions[provider.id] = credential.version
        if provider.kind == "openai":
            return OpenAIProvider(api_key=credential.api_key, base_url=provider.base_url, enabled=True)
        if provider.kind == "google":
            return GeminiProvider(api_key=credential.api_key, enabled=True)
        if provider.kind == "openrouter":
            return OpenRouterProvider(api_key=credential.api_key, base_url=provider.base_url, enabled=True)
        if provider.kind == "anthropic":
            return AnthropicProvider(api_key=credential.api_key, base_url=provider.base_url, enabled=True,
                model_profiles=model_profiles or {})
        return None

    async def _build_local(self, provider: Provider) -> LocalProvider:
        # The key is optional (Ollama needs none); a customer endpoint usually has one.
        api_key = None
        if self.resolver is not None:
            try:
                credential = await self.resolver.resolve_provider(provider.id)
            except CredentialUnavailableError:
                credential = None
            if credential is not None:
                api_key = credential.api_key
                self.credential_versions[provider.id] = credential.version
        # Admin-managed (system) servers are trusted to sit on the private network; a customer's
        # endpoint is held to the public-address rule unless the deployment allows private ones.
        allow_private = (
            getattr(provider, "owner_user_id", None) is None or self.settings.gateway_local_endpoints_allow_private
        )
        return LocalProvider(base_url=provider.base_url, api_key=api_key, allow_private=allow_private)


def _snapshot_anthropic_model_profile(profile: Model | dict[str, Any]) -> dict[str, Any]:
    """Detach Anthropic request constraints from the SQLAlchemy session lifecycle."""
    if isinstance(profile, dict):
        max_output_tokens = profile.get("max_output_tokens")
        feature_metadata = profile.get("feature_metadata_json", profile.get("feature_metadata"))
    else:
        max_output_tokens = profile.max_output_tokens
        feature_metadata = profile.feature_metadata_json
    return {
        "max_output_tokens": max_output_tokens,
        "feature_metadata_json": deepcopy(feature_metadata),
    }
