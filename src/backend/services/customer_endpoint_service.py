"""Customers' own OpenAI-compatible model servers (bring your own endpoint, "BYO").

An endpoint is a ``local`` target connection owned by one user (``Provider.owner_user_id``) plus
the models it serves. Only that user's pools can route to it; it is never part of the shared
catalog (see ``routing_catalog.visible_to``). Its models are free: tokens are not billed, since
the customer pays for the server.

On a hosted deployment (``gateway_local_endpoints_allow_private`` off) the URL must be HTTPS and
resolve to public addresses only, checked here when saved and by the AI backend before each call.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.customer_endpoints import EndpointCreate, EndpointModelCreate, EndpointModelRead, EndpointRead
from src.backend.services.model_probe_service import ModelProbeService
from src.backend.services.pool_rules import OFFLINE_POOL_PURPOSE
from src.backend.services.provider_connection_service import ProviderConnectionService
from src.shared.config import get_settings
from src.shared.models.core import Application, utc_now
from src.shared.models.gateway import ApplicationGatewayConfig, Model, ModelPool, ModelPoolMember, Provider
from src.shared.models.provider_credentials import ProviderCredential
from src.shared.repositories.audit import AuditRepository
from src.shared.services.credential_crypto import CredentialCrypto
from src.shared.services.local_endpoints import (
    LocalEndpointError,
    ensure_endpoint_reachable,
    normalize_local_base_url,
)
from src.shared.services.provider_credential_resolver import CredentialUnavailableError, ProviderCredentialResolver

MAX_ENDPOINTS_PER_USER = 10
MAX_MODELS_PER_ENDPOINT = 20
DISCOVERY_TIMEOUT_SECONDS = 10.0
DISCOVERY_MAX_BYTES = 1_000_000
DISCOVERY_MAX_MODELS = 200
# Free for the gateway: the customer pays for the server. The version marks the source.
BYO_PRICING_VERSION = "byo-free"


class EndpointNotFoundError(LookupError):
    pass


class EndpointInvalidError(ValueError):
    pass


class EndpointConflictError(RuntimeError):
    pass


class CustomerEndpointService:
    discovery_transport: httpx.AsyncBaseTransport | None = None  # tests swap in a mock server

    def __init__(self, session: AsyncSession, crypto: CredentialCrypto | None):
        self.session = session
        self.crypto = crypto
        self.audit = AuditRepository(session)

    async def list(self, owner_user_id: str) -> list[EndpointRead]:
        providers = (
            await self.session.scalars(
                select(Provider).where(Provider.owner_user_id == owner_user_id).order_by(Provider.created_at)
            )
        ).all()
        return [await self._read(provider) for provider in providers]

    async def get(self, owner_user_id: str, endpoint_id: str) -> EndpointRead:
        return await self._read(await self._owned(owner_user_id, endpoint_id))

    async def create(self, owner_user_id: str, payload: EndpointCreate) -> EndpointRead:
        count = await self.session.scalar(
            select(func.count()).select_from(Provider).where(Provider.owner_user_id == owner_user_id)
        )
        if (count or 0) >= MAX_ENDPOINTS_PER_USER:
            raise EndpointConflictError(f"At most {MAX_ENDPOINTS_PER_USER} endpoints per account.")
        allow_private = get_settings().gateway_local_endpoints_allow_private
        try:
            base_url = normalize_local_base_url(payload.base_url, allow_private=allow_private)
            await ensure_endpoint_reachable(base_url, allow_private=allow_private)
        except LocalEndpointError as exc:
            raise EndpointInvalidError(str(exc)) from exc
        api_key = payload.api_key.get_secret_value().strip() if payload.api_key is not None else None
        if api_key is not None and self.crypto is None:
            raise EndpointInvalidError("API keys cannot be stored right now; try again later.")

        provider_id = str(uuid4())
        provider = Provider(
            id=provider_id,
            # Connection names are global and key the executor's clients, so a customer
            # endpoint gets a generated one; the customer only picks the display name.
            name=f"byo-{provider_id[:12]}",
            display_name=payload.display_name,
            kind="local",
            purpose="target",
            status="active",
            base_url=base_url,
            secret_env_var="",
            owner_user_id=owner_user_id,
        )
        self.session.add(provider)
        if api_key is not None:
            await self.session.flush()
            credentials = ProviderConnectionService(self.session, self.crypto)
            self.session.add(credentials._new_credential(provider, api_key, owner_user_id))
        await self.session.flush()
        await self.audit.add_event(
            actor_user_id=owner_user_id,
            action="customer_endpoint.created",
            resource_type="provider_connection",
            resource_id=provider.id,
            metadata={"credential_configured": api_key is not None},
        )
        await self.session.commit()
        return await self._read(provider)

    async def delete(self, owner_user_id: str, endpoint_id: str) -> None:
        provider = await self._owned(owner_user_id, endpoint_id)
        model_ids = list(await self.session.scalars(select(Model.id).where(Model.provider_id == provider.id)))
        await self._ensure_unused(owner_user_id, model_ids)
        await self.session.execute(delete(ProviderCredential).where(ProviderCredential.provider_id == provider.id))
        await self.session.execute(delete(Model).where(Model.provider_id == provider.id))
        await self.session.delete(provider)
        await self.audit.add_event(
            actor_user_id=owner_user_id,
            action="customer_endpoint.deleted",
            resource_type="provider_connection",
            resource_id=provider.id,
        )
        await self.session.commit()

    async def discover_models(self, owner_user_id: str, endpoint_id: str) -> list[str]:
        """The model names the server lists at GET {base_url}/models (OpenAI format), to pick from instead of typing.

        Same guard as saving and calling the endpoint: the address is re-checked first and redirects are not
        followed. The body is capped, and only model ids are read back.
        """
        provider = await self._owned(owner_user_id, endpoint_id)
        allow_private = get_settings().gateway_local_endpoints_allow_private
        try:
            base_url = normalize_local_base_url(provider.base_url or "", allow_private=allow_private)
            await ensure_endpoint_reachable(base_url, allow_private=allow_private)
        except LocalEndpointError as exc:
            raise EndpointInvalidError(str(exc)) from exc
        headers = {}
        if self.crypto is not None:
            try:
                credential = await ProviderCredentialResolver(self.session, self.crypto).resolve_provider(provider.id)
                headers["Authorization"] = f"Bearer {credential.api_key}"
            except CredentialUnavailableError:
                pass  # no key stored (Ollama needs none)
        try:
            async with httpx.AsyncClient(
                timeout=DISCOVERY_TIMEOUT_SECONDS, follow_redirects=False, transport=self.discovery_transport
            ) as client:
                response = await client.get(base_url.rstrip("/") + "/models", headers=headers)
        except httpx.HTTPError as exc:
            raise EndpointInvalidError("The server did not answer GET /models; check that it is running.") from exc
        if response.status_code != 200:
            raise EndpointInvalidError(f"The server answered GET /models with HTTP {response.status_code}.")
        if len(response.content) > DISCOVERY_MAX_BYTES:
            raise EndpointInvalidError("The server's model list is too large.")
        try:
            items = response.json()["data"]
            names = {item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)}
        except (ValueError, KeyError, TypeError) as exc:
            raise EndpointInvalidError("The server's GET /models answer is not OpenAI-compatible.") from exc
        return sorted(name for name in names if 0 < len(name) <= 150)[:DISCOVERY_MAX_MODELS]

    async def add_model(self, owner_user_id: str, endpoint_id: str, payload: EndpointModelCreate) -> EndpointRead:
        provider = await self._owned(owner_user_id, endpoint_id)
        count = await self.session.scalar(
            select(func.count()).select_from(Model).where(Model.provider_id == provider.id)
        )
        if (count or 0) >= MAX_MODELS_PER_ENDPOINT:
            raise EndpointConflictError(f"At most {MAX_MODELS_PER_ENDPOINT} models per endpoint.")
        if await self.session.scalar(
            select(Model.id).where(Model.provider_id == provider.id, Model.name == payload.name)
        ):
            raise EndpointConflictError("This endpoint already has a model with that name.")
        model = Model(
            provider_id=provider.id,
            name=payload.name,
            display_name=payload.display_name or payload.name,
            # Inactive until a probe reaches it on the customer's server.
            status="inactive",
            # "text" satisfies coding and reasoning requests (routing aliases), so any chat model
            # can serve them; capabilities routing filters on strictly must be declared.
            capabilities_json=list(dict.fromkeys(["chat", "text", *payload.capabilities])),
            context_window=payload.context_window,
            input_price_per_million=0.0,
            output_price_per_million=0.0,
            pricing_version=BYO_PRICING_VERSION,
            quality_tier=payload.quality_tier,
            health_status="unknown",
        )
        self.session.add(model)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise EndpointConflictError("This endpoint already has a model with that name.") from exc
        await self.audit.add_event(
            actor_user_id=owner_user_id,
            action="customer_endpoint.model_added",
            resource_type="model",
            resource_id=model.id,
            metadata={"endpoint_id": provider.id},
        )
        await self.session.commit()
        return await self._read(provider)

    async def probe_model(
        self, owner_user_id: str, endpoint_id: str, model_id: str, *, probe_service: ModelProbeService | None = None
    ) -> EndpointRead:
        """Calls the model once through the AI backend; a healthy model becomes active, a failing one inactive."""
        provider = await self._owned(owner_user_id, endpoint_id)
        model = await self._owned_model(provider, model_id)
        result = await (probe_service or ModelProbeService(self.session)).probe(model.id)
        await self.session.refresh(model)
        model.status = "active" if result.status == "healthy" else "inactive"
        model.updated_at = utc_now()
        await self.session.commit()
        return await self._read(provider)

    async def delete_model(self, owner_user_id: str, endpoint_id: str, model_id: str) -> EndpointRead:
        provider = await self._owned(owner_user_id, endpoint_id)
        model = await self._owned_model(provider, model_id)
        await self._ensure_unused(owner_user_id, [model.id])
        await self.session.delete(model)
        await self.audit.add_event(
            actor_user_id=owner_user_id,
            action="customer_endpoint.model_deleted",
            resource_type="model",
            resource_id=model.id,
            metadata={"endpoint_id": provider.id},
        )
        await self.session.commit()
        return await self._read(provider)

    async def _owned(self, owner_user_id: str, endpoint_id: str) -> Provider:
        provider = await self.session.get(Provider, endpoint_id)
        # Another user's endpoint (or a system connection) reads as missing.
        if provider is None or provider.owner_user_id != owner_user_id:
            raise EndpointNotFoundError("Endpoint not found.")
        return provider

    async def _owned_model(self, provider: Provider, model_id: str) -> Model:
        model = await self.session.get(Model, model_id)
        if model is None or model.provider_id != provider.id:
            raise EndpointNotFoundError("Model not found.")
        return model

    async def _ensure_unused(self, owner_user_id: str, model_ids: list[str]) -> None:
        if not model_ids:
            return
        pools = (
            await self.session.execute(
                select(ModelPool.id, ModelPool.name, ModelPool.purpose)
                .join(ModelPoolMember, ModelPoolMember.pool_id == ModelPool.id)
                .where(ModelPoolMember.model_id.in_(model_ids))
                .distinct()
            )
        ).all()
        if pools:
            # An offline pipeline's pool is internal ("offline:<slug>"): name the pipeline instead.
            offline_ids = [pool.id for pool in pools if pool.purpose == OFFLINE_POOL_PURPOSE]
            pipelines = (
                await self.session.scalars(
                    select(Application.name)
                    .join(ApplicationGatewayConfig, ApplicationGatewayConfig.application_id == Application.id)
                    .where(ApplicationGatewayConfig.model_pool_id.in_(offline_ids))
                    .order_by(Application.name)
                )
            ).all() if offline_ids else []
            other_pools = sorted(pool.name for pool in pools if pool.purpose != OFFLINE_POOL_PURPOSE)
            users = []
            if pipelines:
                users.append("offline pipelines " + ", ".join(pipelines))
            if other_pools:
                users.append("model pools " + ", ".join(other_pools))
            raise EndpointConflictError(
                f"In use by {' and '.join(users)}. Delete them or switch them to other models first."
            )
        # Only the owner's applications can name these models (configuration validation).
        configs = (
            await self.session.scalars(
                select(ApplicationGatewayConfig)
                .join(Application, Application.id == ApplicationGatewayConfig.application_id)
                .where(Application.owner_user_id == owner_user_id)
            )
        ).all()
        as_fallback = any(
            config.fallback_model_id in model_ids or set(config.fallback_model_ids_json or []) & set(model_ids)
            for config in configs
        )
        if as_fallback:
            raise EndpointConflictError("Remove these models from your applications' fallbacks first.")

    async def _read(self, provider: Provider) -> EndpointRead:
        credential = await self.session.scalar(
            select(ProviderCredential).where(ProviderCredential.provider_id == provider.id)
        )
        models = (
            await self.session.scalars(select(Model).where(Model.provider_id == provider.id).order_by(Model.created_at))
        ).all()
        has_key = credential is not None and credential.status == "active"
        return EndpointRead(
            id=provider.id,
            display_name=provider.display_name,
            base_url=provider.base_url or "",
            status=provider.status,
            has_api_key=has_key,
            key_last4=credential.key_last4 if has_key else None,
            models=[
                EndpointModelRead(
                    id=model.id,
                    name=model.name,
                    display_name=model.display_name,
                    quality_tier=model.quality_tier,
                    context_window=model.context_window,
                    capabilities=list(model.capabilities_json or []),
                    status=model.status,
                    health_status=model.health_status,
                    last_probe_at=model.last_probe_at,
                    last_probe_latency_ms=model.last_probe_latency_ms,
                    last_probe_error=model.last_probe_error,
                )
                for model in models
            ],
            created_at=provider.created_at,
            updated_at=provider.updated_at,
        )
