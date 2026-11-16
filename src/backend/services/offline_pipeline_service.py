"""Offline pipelines: create, list, edit and delete.

An offline pipeline is stored as an application with ``pipeline = "offline"`` plus its own model pool (purpose
``offline_pipeline``) of exactly three self-hosted models, one per tier. The AI backend runs every request of such an
application on the offline pipeline (pipeline_profile.settings_for_application), so one process serves applications
and offline pipelines side by side. Only this service changes an offline pipeline's pool and gateway configuration:
the generic pool and gateway-config APIs refuse them, so no external model can be added later.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.applications import ApplicationCreate, ApplicationUpdate
from src.backend.dtos.catalog import ModelPoolCreate, ModelPoolMemberCreate
from src.backend.dtos.gateway_configuration import GatewayConfigUpsert
from src.backend.dtos.offline_pipelines import (
    OfflineModelRead,
    OfflinePipelineCreate,
    OfflinePipelineRead,
    OfflinePipelineUpdate,
)
from src.backend.repositories.applications import ApplicationRepository
from src.backend.services.application_service import (
    ApplicationConflictError,
    ApplicationService,
    ApplicationStateError,
)
from src.backend.services.catalog_service import CatalogConflictError, CatalogReferenceError, CatalogService
from src.backend.services.gateway_configuration_service import GatewayConfigurationService
from src.backend.services.pool_rules import OFFLINE_POOL_PURPOSE
from src.shared.config import Settings, get_settings
from src.shared.models.core import Application
from src.shared.models.gateway import ApplicationGatewayConfig, Model, ModelPool, ModelPoolMember, Provider
from src.shared.services.local_endpoints import is_local_endpoint

TIERS = ("simple", "mid", "hard")
TIER_NAMES = {"simple": "Small", "mid": "Mid", "hard": "Large"}
# What the offline pipeline runs at each stage; the profile would map anything else onto these anyway.
STRATEGIES = {
    "default_understander": "classifier",
    "default_decomposer": "none",
    "default_router": "database",
    "default_aggregator": "deterministic",
}


class OfflinePipelineNotFoundError(Exception):
    pass


class OfflinePipelineConflictError(Exception):
    pass


class OfflinePipelineInvalidError(Exception):
    """Shown to the user as is."""


class OfflinePipelineService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()
        self.applications = ApplicationService(session)
        self.configuration = GatewayConfigurationService(session)
        self.repository = ApplicationRepository(session)

    # ---- reads -----------------------------------------------------------------------------------------

    async def list(self, owner_user_id: str) -> list[OfflinePipelineRead]:
        rows = await self.session.scalars(
            select(Application)
            .where(
                Application.owner_user_id == owner_user_id,
                Application.pipeline == "offline",
                Application.status != "archived",
            )
            .order_by(Application.created_at.desc(), Application.id)
        )
        return [await self._read(application) for application in rows]

    async def get(self, owner_user_id: str, pipeline_id: str) -> OfflinePipelineRead:
        return await self._read(await self._owned(owner_user_id, pipeline_id))

    # ---- writes ----------------------------------------------------------------------------------------

    async def create(self, owner_user_id: str, payload: OfflinePipelineCreate) -> OfflinePipelineRead:
        await self._validated_ladder(owner_user_id, payload.models.model_dump())
        if await self.repository.get_by_name(payload.name) is not None:
            raise OfflinePipelineConflictError("A pipeline or application with this name already exists.")
        if await self.repository.get_by_slug(payload.slug) is not None:
            raise OfflinePipelineConflictError("A pipeline or application with this slug already exists.")

        catalog = CatalogService(self.session)
        try:
            pool = await catalog.create_pool(
                ModelPoolCreate(
                    name=f"offline:{payload.slug}"[:100],
                    purpose=OFFLINE_POOL_PURPOSE,
                    status="active",
                    members=[ModelPoolMemberCreate(model_id=model_id) for model_id in payload.models.model_dump().values()],
                ),
                owner_user_id,
                enforce_tier_rule=True,
            )
        except (CatalogConflictError, CatalogReferenceError) as exc:
            raise OfflinePipelineInvalidError(str(exc)) from exc

        application = None
        try:
            application = await self.applications.create(
                owner_user_id,
                ApplicationCreate(
                    slug=payload.slug, name=payload.name, description=payload.description, environment="production"
                ),
                initialize_gateway=False,
                pipeline="offline",
            )
            await self.configuration.upsert(
                owner_user_id,
                application.id,
                GatewayConfigUpsert(
                    model_pool_id=pool.id,
                    **STRATEGIES,
                    max_output_tokens=payload.max_output_tokens,
                    timeout_seconds=payload.timeout_seconds,
                    fallback_enabled=payload.fallback_enabled,
                    classifier_mid_difficulty=payload.classifier_mid_difficulty,
                    classifier_large_difficulty=payload.classifier_large_difficulty,
                ),
                offline_pipeline=True,
            )
        except ApplicationConflictError as exc:
            await self._drop_pool(pool.id)
            raise OfflinePipelineConflictError(str(exc)) from exc
        except Exception:
            if application is not None:
                await self.applications.delete(owner_user_id, application.id, offline_pipeline=True)
            await self._drop_pool(pool.id)
            raise
        return await self.get(owner_user_id, application.id)

    async def update(self, owner_user_id: str, pipeline_id: str, payload: OfflinePipelineUpdate) -> OfflinePipelineRead:
        application = await self._owned(owner_user_id, pipeline_id)
        config = await self._config(application.id)
        changes = payload.model_dump(exclude_unset=True)

        if payload.models is not None:
            ladder = payload.models.model_dump()
            await self._validated_ladder(owner_user_id, ladder)
            await self.session.execute(delete(ModelPoolMember).where(ModelPoolMember.pool_id == config.model_pool_id))
            for model_id in ladder.values():
                self.session.add(ModelPoolMember(pool_id=config.model_pool_id, model_id=model_id, role="primary"))
            await self.session.commit()

        app_fields = {key: changes[key] for key in ("name", "description", "status") if key in changes}
        if app_fields:
            try:
                await self.applications.update(owner_user_id, application.id, ApplicationUpdate(**app_fields))
            except ApplicationConflictError as exc:
                raise OfflinePipelineConflictError(str(exc)) from exc
            except ApplicationStateError as exc:
                raise OfflinePipelineInvalidError(str(exc)) from exc

        config_fields = {
            key: changes[key]
            for key in ("classifier_mid_difficulty", "classifier_large_difficulty", "max_output_tokens",
                        "timeout_seconds", "fallback_enabled")
            if key in changes
        }
        if config_fields or payload.models is not None:
            current = await self.configuration.get(owner_user_id, application.id)
            values = current.model_dump(exclude={"id", "application_id", "created_at", "updated_at", "version"})
            values.update(config_fields)
            values.update(STRATEGIES, model_ids=[], fallback_model_ids=[], fallback_model_id=None, expected_version=None)
            await self.configuration.upsert(
                owner_user_id, application.id, GatewayConfigUpsert(**values), offline_pipeline=True
            )
        return await self.get(owner_user_id, application.id)

    async def delete(self, owner_user_id: str, pipeline_id: str) -> None:
        application = await self._owned(owner_user_id, pipeline_id)
        config = await self._config(application.id)
        pool_id = config.model_pool_id
        await self.applications.delete(owner_user_id, application.id, offline_pipeline=True)
        if pool_id is not None:
            await self._drop_pool(pool_id)

    # ---- helpers ---------------------------------------------------------------------------------------

    async def _owned(self, owner_user_id: str, pipeline_id: str) -> Application:
        application = await self.repository.get_by_id(owner_user_id, pipeline_id)
        if application is None or application.pipeline != "offline":
            raise OfflinePipelineNotFoundError("Offline pipeline not found")
        return application

    async def _config(self, application_id: str) -> ApplicationGatewayConfig:
        config = await self.repository.gateway_config(application_id)
        if config is None or config.model_pool_id is None:
            raise OfflinePipelineNotFoundError("Offline pipeline configuration not found")
        return config

    async def _validated_ladder(self, owner_user_id: str, ladder: dict[str, str]) -> None:
        """Three distinct models, each in its tier, each active (probed) on a self-hosted endpoint the user can see."""
        if len(set(ladder.values())) != len(TIERS):
            raise OfflinePipelineInvalidError("Pick three different models, one per tier.")
        for tier in TIERS:
            model = await self.session.get(Model, ladder[tier])
            provider = await self.session.get(Provider, model.provider_id) if model is not None else None
            # Another user's endpoint reads as missing, so its existence is not revealed.
            if model is None or provider is None or provider.owner_user_id not in (None, owner_user_id):
                raise OfflinePipelineInvalidError(f"{TIER_NAMES[tier]}: model not found.")
            if not is_local_endpoint(provider) or provider.status != "active" or provider.purpose != "target":
                raise OfflinePipelineInvalidError(
                    f"{TIER_NAMES[tier]}: {model.name} is not on a self-hosted endpoint; an offline pipeline only "
                    "runs models from My endpoints."
                )
            if model.status != "active":
                raise OfflinePipelineInvalidError(
                    f"{TIER_NAMES[tier]}: probe {model.name} on My endpoints first; only models that answered can be used."
                )
            if model.quality_tier != tier:
                raise OfflinePipelineInvalidError(
                    f"{TIER_NAMES[tier]}: {model.name} is registered as "
                    f"{TIER_NAMES.get(model.quality_tier or '', 'no tier')}; pick a model of this tier."
                )

    async def _drop_pool(self, pool_id: str) -> None:
        await self.session.execute(delete(ModelPoolMember).where(ModelPoolMember.pool_id == pool_id))
        await self.session.execute(
            delete(ModelPool).where(ModelPool.id == pool_id, ModelPool.purpose == OFFLINE_POOL_PURPOSE)
        )
        await self.session.commit()

    async def _read(self, application: Application) -> OfflinePipelineRead:
        config = await self.repository.gateway_config(application.id)
        models: list[OfflineModelRead] = []
        if config is not None and config.model_pool_id is not None:
            rows = await self.session.execute(
                select(Model, Provider)
                .join(ModelPoolMember, ModelPoolMember.model_id == Model.id)
                .join(Provider, Provider.id == Model.provider_id)
                .where(ModelPoolMember.pool_id == config.model_pool_id, ModelPoolMember.role == "primary")
            )
            order = {tier: index for index, tier in enumerate(TIERS)}
            for model, provider in sorted(rows.all(), key=lambda row: order.get(row[0].quality_tier or "", 9)):
                models.append(OfflineModelRead(
                    tier=model.quality_tier if model.quality_tier in TIERS else "mid",
                    model_id=model.id, name=model.name, display_name=model.display_name,
                    endpoint_id=provider.id, endpoint_name=provider.display_name or provider.name,
                    status=model.status, health_status=model.health_status or "unknown",
                    context_window=model.context_window,
                ))
        own_mid = config.classifier_mid_difficulty if config else None
        own_large = config.classifier_large_difficulty if config else None
        return OfflinePipelineRead(
            id=application.id,
            slug=application.slug,
            name=application.name,
            description=application.description,
            status=application.status,
            models=models,
            classifier_mid_difficulty=own_mid if own_mid is not None else self.settings.gateway_classifier_mid_difficulty,
            classifier_large_difficulty=(
                own_large if own_large is not None else self.settings.gateway_classifier_large_difficulty
            ),
            thresholds_default=own_mid is None and own_large is None,
            max_output_tokens=config.max_output_tokens if config else 800,
            timeout_seconds=config.timeout_seconds if config else 60,
            fallback_enabled=config.fallback_enabled if config else True,
            request_count=await self.repository.request_count(application.id),
            config_version=config.version if config else None,
            created_at=application.created_at,
            updated_at=application.updated_at,
        )
