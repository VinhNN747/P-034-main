from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.gateway_configuration import GatewayConfigRead, GatewayConfigUpsert
from src.backend.services.catalog_service import CatalogReferenceError
from src.backend.services.pool_rules import OFFLINE_POOL_PURPOSE
from src.shared.models.core import Application
from src.shared.models.gateway import (
    ApplicationGatewayConfig,
    Model,
    ModelPool,
    ModelPoolMember,
    RoutingPolicy,
)
from src.shared.repositories.audit import AuditRepository
from src.shared.repositories.routing_catalog import is_system_pool, visible_to


class GatewayConfigConflictError(Exception):
    pass


class GatewayConfigurationService:
    """Manage the normalized application gateway configuration contract."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.audit = AuditRepository(session)

    async def _owned_application(self, owner_user_id: str, application_id: str) -> Application:
        application = await self.session.scalar(
            select(Application).where(
                Application.id == application_id,
                Application.owner_user_id == owner_user_id,
            )
        )
        if application is None:
            raise CatalogReferenceError("Application not found")
        return application

    async def _resolve_pool_and_policy(
        self, owner_user_id: str, payload: GatewayConfigUpsert,
        config: ApplicationGatewayConfig | None,
    ) -> tuple[ModelPool | None, RoutingPolicy | None]:
        pool_id = (payload.model_pool_id if "model_pool_id" in payload.model_fields_set
                   else config.model_pool_id if config else None)
        policy_id = (payload.routing_policy_id if "routing_policy_id" in payload.model_fields_set
                     else config.routing_policy_id if config else None)
        pool = await self.session.get(ModelPool, pool_id) if pool_id else None
        if pool_id is not None and (
            pool is None or pool.status != "active"
            # A system pool (default or task template) can be chosen by any owner.
            or (pool.owner_user_id != owner_user_id and not is_system_pool(pool))
        ):
            raise CatalogReferenceError("Active model pool not found")
        policy = await self.session.get(RoutingPolicy, policy_id) if policy_id else None
        if policy_id is not None and (policy is None or policy.status != "active"):
            raise CatalogReferenceError("Active routing policy not found")
        return pool, policy

    async def _active_model_ids(self, pool_id: str | None, owner_user_id: str) -> list[str]:
        from src.shared.models.gateway import Provider
        query = select(Model.id).join(Provider, Provider.id == Model.provider_id).where(
            Model.status == "active", Provider.status == "active",
            # The owner's own endpoints count only through a pool, as routing does.
            visible_to(owner_user_id if pool_id is not None else None),
        )
        if pool_id is not None:
            query = query.join(ModelPoolMember, ModelPoolMember.model_id == Model.id).where(
                ModelPoolMember.pool_id == pool_id,
            )
        return list(await self.session.scalars(query.order_by(Model.id)))

    async def _validate_model_ids(self, model_ids: list[str], fallback_ids: list[str]) -> None:
        if set(model_ids).intersection(fallback_ids):
            raise CatalogReferenceError("Primary and fallback models must be distinct")
        for model_id in [*model_ids, *fallback_ids]:
            model = await self.session.get(Model, model_id)
            if model is None or model.status != "active":
                raise CatalogReferenceError(f"Model '{model_id}' not found or inactive")

    async def upsert(
        self,
        owner_user_id: str,
        application_id: str,
        payload: GatewayConfigUpsert,
        *,
        offline_pipeline: bool = False,
    ) -> GatewayConfigRead:
        """``offline_pipeline``: the caller is OfflinePipelineService, which enforces the offline rules
        (local models only). Through any other path an offline pipeline's configuration stays read-only."""
        application = await self._owned_application(owner_user_id, application_id)
        if application.pipeline == "offline" and not offline_pipeline:
            raise GatewayConfigConflictError("This is an offline pipeline; change it under Offline pipelines.")
        config = await self.session.scalar(
            select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == application_id)
        )
        if config is not None and payload.expected_version is not None:
            if config.version != payload.expected_version:
                raise GatewayConfigConflictError("Gateway configuration version is stale")

        pool, policy = await self._resolve_pool_and_policy(owner_user_id, payload, config)
        if pool is not None and pool.purpose == OFFLINE_POOL_PURPOSE and application.pipeline != "offline":
            # An offline pipeline's pool is not shared with applications.
            raise CatalogReferenceError("Active model pool not found")
        pool_id = pool.id if pool else None
        policy_id = policy.id if policy else None
        candidate_ids = await self._active_model_ids(pool_id, owner_user_id)
        model_ids = payload.model_ids or candidate_ids
        fallback_ids = list(payload.fallback_model_ids)
        if not fallback_ids and payload.fallback_model_id:
            fallback_ids = [payload.fallback_model_id]
        if not payload.model_ids:
            model_ids = [model_id for model_id in model_ids if model_id not in fallback_ids]
        await self._validate_model_ids(model_ids, fallback_ids)
        if not set([*model_ids, *fallback_ids]).issubset(candidate_ids):
            raise CatalogReferenceError("Models must belong to the selected routing candidate set")

        if config is None:
            config = ApplicationGatewayConfig(
                application_id=application_id,
                model_pool_id=pool_id,
                routing_policy_id=policy_id,
                version=1,
            )
            self.session.add(config)
        else:
            config.model_pool_id = pool_id
            config.routing_policy_id = policy_id
            config.version += 1

        config.budget_ceiling = payload.budget_ceiling
        config.context_window = payload.context_window
        config.fallback_model_id = fallback_ids[0] if fallback_ids else None
        config.default_understander = payload.default_understander
        config.default_decomposer = payload.default_decomposer
        config.default_router = payload.default_router
        config.default_aggregator = payload.default_aggregator
        config.routing_objective = payload.routing_objective
        config.fallback_model_ids_json = fallback_ids
        config.warning_threshold = payload.warning_threshold
        config.budget_action = payload.budget_action
        config.max_output_tokens = payload.max_output_tokens
        config.max_latency_ms = payload.max_latency_ms
        config.timeout_seconds = payload.timeout_seconds
        config.retry_limit = payload.retry_limit
        config.fallback_enabled = payload.fallback_enabled
        config.classifier_mid_difficulty = payload.classifier_mid_difficulty
        config.classifier_large_difficulty = payload.classifier_large_difficulty

        await self.session.flush()
        await self.audit.add_event(
            actor_user_id=owner_user_id,
            action="application.gateway_config.updated",
            resource_type="application_gateway_config",
            resource_id=config.id,
        )
        await self.session.commit()
        await self.session.refresh(config)
        return self.to_read(config, model_ids)

    async def get(self, owner_user_id: str, application_id: str) -> GatewayConfigRead:
        await self._owned_application(owner_user_id, application_id)
        config = await self.session.scalar(
            select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == application_id)
        )
        if config is None:
            raise CatalogReferenceError("Gateway configuration not found")
        return self.to_read(config, await self._active_model_ids(config.model_pool_id, owner_user_id))

    @staticmethod
    def to_read(config: ApplicationGatewayConfig, model_ids: list[str]) -> GatewayConfigRead:
        return GatewayConfigRead(
            id=config.id,
            application_id=config.application_id,
            model_pool_id=config.model_pool_id,
            routing_policy_id=config.routing_policy_id,
            budget_ceiling=config.budget_ceiling,
            context_window=config.context_window,
            fallback_model_id=config.fallback_model_id,
            default_understander=config.default_understander,
            default_decomposer=config.default_decomposer,
            default_router=config.default_router,
            default_aggregator=config.default_aggregator,
            model_ids=model_ids,
            fallback_model_ids=config.fallback_model_ids_json or [],
            routing_objective=config.routing_objective,
            warning_threshold=config.warning_threshold,
            budget_action=config.budget_action,
            max_output_tokens=config.max_output_tokens,
            max_latency_ms=config.max_latency_ms,
            timeout_seconds=config.timeout_seconds,
            retry_limit=config.retry_limit,
            fallback_enabled=config.fallback_enabled,
            classifier_mid_difficulty=config.classifier_mid_difficulty,
            classifier_large_difficulty=config.classifier_large_difficulty,
            version=config.version,
            created_at=config.created_at,
            updated_at=config.updated_at,
        )
