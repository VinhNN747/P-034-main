from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.dtos.openai_compat import ModelCard, ModelList
from src.backend_ai.gateway.model_alias import ALIAS_DESCRIPTIONS
from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.shared.config import Settings, get_settings
from src.shared.models.gateway import ApplicationGatewayConfig, Model, Provider
from src.shared.repositories.provider_credentials import ProviderCredentialRepository
from src.shared.repositories.routing_catalog import routing_candidates
from src.shared.services.local_endpoints import is_local_endpoint


class OpenAIModelService:
    """Resolve the same application-visible model cards for list and retrieve."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    async def _active_rows(self, principal: ApplicationPrincipal) -> list[tuple[object, Model, Provider]]:
        try:
            config = await self.session.scalar(
                select(ApplicationGatewayConfig).where(
                    ApplicationGatewayConfig.application_id == principal.application_id
                )
            )
            if config is None:
                return []
            rows = await routing_candidates(self.session, config.model_pool_id)
        except SQLAlchemyError as exc:
            raise OpenAIAPIError(
                503,
                "The model catalog could not be loaded.",
                type="server_error",
                code="model_catalog_unavailable",
            ) from exc
        rows = [
            (member, model, provider)
            for member, model, provider in rows
            if model.status == "active"
            and provider.status == "active"
            and getattr(provider, "purpose", "target") == "target"
        ]
        if self.settings.gateway_execution_mode == "offline" or getattr(principal, "pipeline", "online") == "offline":
            # An offline pipeline's key lists only what it can run: self-hosted endpoints (and the stub).
            return [row for row in rows if getattr(row[2], "kind", "offline") in {"offline", "local"}]
        try:
            configured_ids = await ProviderCredentialRepository(self.session).configured_provider_ids()
        except SQLAlchemyError as exc:
            raise OpenAIAPIError(
                503,
                "The model catalog could not be loaded.",
                type="server_error",
                code="model_catalog_unavailable",
            ) from exc
        enabled_kinds = set()
        if self.settings.gateway_openai_enabled:
            enabled_kinds.add("openai")
        if self.settings.gateway_gemini_enabled:
            enabled_kinds.add("google")
        if self.settings.gateway_openrouter_enabled:
            enabled_kinds.add("openrouter")
        if self.settings.gateway_anthropic_enabled:
            enabled_kinds.add("anthropic")
        return [
            row
            for row in rows
            if row[2].id in configured_ids
            and (getattr(row[2], "kind", None) in enabled_kinds or is_local_endpoint(row[2]))
        ]

    async def list_visible(self, principal: ApplicationPrincipal) -> ModelList:
        rows = await self._active_rows(principal)
        reserved_ids = set(ALIAS_DESCRIPTIONS)
        concrete_names = sorted({model.name for _, model, _ in rows} - reserved_ids)
        cards = [ModelCard(id=alias, owned_by="gateway-router") for alias in ALIAS_DESCRIPTIONS]
        created_by_name = {model.name: int(model.created_at.timestamp()) for _, model, _ in rows}
        cards.extend(
            ModelCard(id=name, owned_by="gateway-pinned", created=created_by_name[name]) for name in concrete_names
        )
        return ModelList(data=cards)

    async def retrieve_visible(self, principal: ApplicationPrincipal, model: str) -> ModelCard:
        cards = await self.list_visible(principal)
        card = next((item for item in cards.data if item.id == model), None)
        if card is None:
            raise OpenAIAPIError(
                404,
                "The requested model was not found in this application's model catalog.",
                code="model_not_found",
                param="model",
            )
        return card

    async def visible_candidates(self, principal: ApplicationPrincipal) -> list[tuple[Model, Provider]]:
        """Return concrete visible rows without de-duplication for capability resolution."""
        return [(model, provider) for _, model, provider in await self._active_rows(principal)]
