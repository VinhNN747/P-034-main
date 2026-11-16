from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.catalog import (
    FallbackSuggestion,
    FallbackSuggestionRequest,
    ModelCreate,
    ModelPoolCreate,
    ModelPoolMemberRead,
    ModelPoolRead,
    ModelPoolUpdate,
    ModelRead,
    PolicyCreate,
    PolicyRead,
    ProviderCreate,
    ProviderRead,
)
from src.backend.repositories.catalog import CatalogRepository
from src.backend.services import pool_rules
from src.backend.services.pool_rules import OFFLINE_POOL_PURPOSE
from src.shared.config import get_settings
from src.shared.models.gateway import (
    ApplicationGatewayConfig,
    Model,
    ModelPool,
    ModelPoolMember,
    Provider,
    RoutingPolicy,
)
from src.shared.repositories.audit import AuditRepository
from src.shared.repositories.provider_credentials import ProviderCredentialRepository
from src.shared.repositories.routing_catalog import is_system_pool, visible_to
from src.shared.services.local_endpoints import LocalEndpointError, normalize_local_base_url


class CatalogConflictError(Exception):
    pass


class CatalogReferenceError(Exception):
    pass


class CatalogService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = CatalogRepository(session)

    async def _callable_target_provider_ids(self) -> set[str]:
        configured_ids = await ProviderCredentialRepository(self.session).configured_provider_ids()
        rows = (
            await self.session.execute(
                select(Provider.id, Provider.kind, Provider.base_url).where(Provider.purpose == "target")
            )
        ).all()
        provider_kinds = {provider_id: kind for provider_id, kind, _ in rows}
        local_ids = {provider_id for provider_id, kind, base_url in rows if kind == "local" and base_url}
        return pool_rules.callable_provider_ids(get_settings(), configured_ids, provider_kinds, local_ids)

    async def create_provider(self, payload: ProviderCreate, actor_user_id: str | None = None) -> ProviderRead:
        expected_base_url = {
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "anthropic": "https://api.anthropic.com",
        }.get(payload.kind)
        if expected_base_url is not None and payload.base_url not in (None, expected_base_url):
            raise CatalogReferenceError("Provider base URL is not an approved endpoint")
        if payload.kind == "local" and payload.base_url is not None:
            try:
                expected_base_url = normalize_local_base_url(payload.base_url)
            except LocalEndpointError as exc:
                raise CatalogReferenceError(str(exc)) from exc
        elif expected_base_url is None and payload.base_url is not None:
            raise CatalogReferenceError("This provider kind does not accept a custom base URL")
        if await self.repository.provider_by_name(payload.name):
            raise CatalogConflictError("Provider name already exists")
        values = payload.model_dump(mode="json")
        values["base_url"] = expected_base_url
        provider = Provider(**values)
        self.session.add(provider)
        try:
            await self.session.flush()
            if actor_user_id is not None:
                await AuditRepository(self.session).add_event(
                    actor_user_id=actor_user_id,
                    action="provider_connection.created",
                    resource_type="provider_connection",
                    resource_id=provider.id,
                    metadata={"kind": provider.kind, "purpose": "target", "credential_configured": False},
                )
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise CatalogConflictError("Provider name already exists") from exc
        await self.session.refresh(provider)
        return self.provider_read(provider)

    async def list_providers(self) -> list[ProviderRead]:
        return [self.provider_read(item) for item in await self.repository.list_providers()]

    async def create_model(self, payload: ModelCreate) -> ModelRead:
        provider = await self.repository.provider(payload.provider_id)
        if provider is None:
            raise CatalogReferenceError("Provider not found")
        if await self.repository.model_by_provider_and_name(payload.provider_id, payload.name):
            raise CatalogConflictError("Model name already exists for provider")
        values = payload.model_dump(mode="json")
        if "status" not in payload.model_fields_set:
            # Internal seed/catalog callers predate the admin probe workflow.
            # Public admin creation uses ModelCatalogService and stays inactive.
            values["status"] = "active"
        values["capabilities_json"] = values.pop("capabilities")
        values["feature_metadata_json"] = values.pop("feature_metadata")
        model = Model(**values)
        self.session.add(model)
        try:
            await self.session.flush()
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise CatalogConflictError("Model name already exists for provider") from exc
        await self.session.refresh(model)
        return self.model_read(model)

    async def list_models(self) -> list[ModelRead]:
        return [self.model_read(item) for item in await self.repository.list_models()]

    async def _checked_members(
        self, payload: ModelPoolCreate, enforce_tier_rule: bool, owner_user_id: str | None
    ) -> None:
        self.validate_member_input(payload)
        members: list[pool_rules.PoolMember] = []
        for member in payload.members:
            model = await self.repository.model(member.model_id)
            provider = await self.session.get(Provider, model.provider_id) if model is not None else None
            # Another user's endpoint reads as missing, so its existence is not revealed.
            if (
                model is None
                or model.status != "active"
                or provider is None
                or provider.owner_user_id not in (None, owner_user_id)
            ):
                raise CatalogReferenceError(f"Active model not found: {member.model_id}")
            members.append(pool_rules.PoolMember(member.role, model, provider))
        if not enforce_tier_rule:
            return
        try:
            pool_rules.validate(members)
        except pool_rules.PoolRuleError as exc:
            raise CatalogReferenceError(str(exc)) from exc

    async def suggest_fallbacks(
        self, payload: FallbackSuggestionRequest, owner_user_id: str | None = None
    ) -> list[FallbackSuggestion]:
        primaries: list[pool_rules.PoolMember] = []
        for model_id in payload.model_ids:
            model = await self.repository.model(model_id)
            provider = await self.session.get(Provider, model.provider_id) if model is not None else None
            if model is None or provider is None or provider.owner_user_id not in (None, owner_user_id):
                raise CatalogReferenceError(f"Model not found: {model_id}")
            primaries.append(pool_rules.PoolMember("primary", model, provider))
        rows = await self.session.execute(
            select(Model, Provider).join(Provider, Provider.id == Model.provider_id).where(visible_to(owner_user_id))
        )
        picks = pool_rules.suggest_fallbacks(
            primaries,
            list(rows.all()),
            await self._callable_target_provider_ids(),
            allow_other_providers=payload.allow_other_providers,
        )
        return [
            FallbackSuggestion(
                for_model_id=primary.model.id,
                tier=primary.model.quality_tier,
                model_id=pick[0].id if pick else None,
                reason=reason,
                output_price_delta_per_million=(
                    float(pick[0].output_price_per_million or 0) - float(primary.model.output_price_per_million or 0)
                    if pick
                    else None
                ),
            )
            for primary, pick, reason in picks
        ]

    async def create_pool(
        self,
        payload: ModelPoolCreate,
        owner_user_id: str,
        *,
        enforce_tier_rule: bool = False,
    ) -> ModelPoolRead:
        """``enforce_tier_rule``: user-facing create/edit (the API) requires 3 primaries
        in 3 tiers; system and seed callers may build other shapes."""
        if await self.repository.pool_by_name(payload.name, owner_user_id):
            raise CatalogConflictError("Model pool name already exists")
        await self._checked_members(payload, enforce_tier_rule, owner_user_id)
        pool = ModelPool(
            owner_user_id=owner_user_id,
            name=payload.name,
            purpose=payload.purpose,
            status=payload.status,
        )
        self.session.add(pool)
        await self.session.flush()
        for member in payload.members:
            self.session.add(
                ModelPoolMember(
                    pool_id=pool.id,
                    model_id=member.model_id,
                    role=member.role,
                )
            )
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise CatalogConflictError("Model pool members must be unique") from exc
        await self.session.refresh(pool)
        return await self.pool_read(pool)

    async def list_pools(self, owner_user_id: str) -> list[ModelPoolRead]:
        """The owner's pools, then the system pools (read-only, selectable for an application)."""
        pools = [*await self.repository.list_pools(owner_user_id), *await self.repository.system_pools()]
        # An offline pipeline's pool belongs to that pipeline (Offline pipelines section), not to this list.
        return [await self.pool_read(item) for item in pools if item.purpose != OFFLINE_POOL_PURPOSE]

    async def get_pool(self, pool_id: str, owner_user_id: str) -> ModelPoolRead:
        pool = await self.repository.pool(pool_id)
        if pool is None or (pool.owner_user_id != owner_user_id and not is_system_pool(pool)):
            raise CatalogReferenceError("Model pool not found")
        return await self.pool_read(pool)

    async def update_pool(
        self,
        pool_id: str,
        payload: ModelPoolUpdate,
        owner_user_id: str,
        *,
        enforce_tier_rule: bool = False,
    ) -> ModelPoolRead:
        pool = await self.repository.pool(pool_id)
        if pool is None or pool.owner_user_id != owner_user_id:
            raise CatalogReferenceError("Model pool not found")
        if pool.purpose == OFFLINE_POOL_PURPOSE:
            raise CatalogConflictError("This pool belongs to an offline pipeline; change it under Offline pipelines.")
        same_name = await self.repository.pool_by_name(payload.name, owner_user_id)
        if same_name is not None and same_name.id != pool.id:
            raise CatalogConflictError("Model pool name already exists")
        await self._checked_members(payload, enforce_tier_rule, owner_user_id)

        pool.name = payload.name
        pool.purpose = payload.purpose
        pool.status = payload.status
        await self.session.execute(delete(ModelPoolMember).where(ModelPoolMember.pool_id == pool.id))
        for member in payload.members:
            self.session.add(
                ModelPoolMember(
                    pool_id=pool.id,
                    model_id=member.model_id,
                    role=member.role,
                )
            )
        try:
            await self.session.flush()
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise CatalogConflictError("Model pool members must be unique") from exc
        await self.session.refresh(pool)
        return await self.pool_read(pool)

    async def delete_pool(self, pool_id: str, owner_user_id: str) -> None:
        pool = await self.session.scalar(
            select(ModelPool)
            .where(ModelPool.id == pool_id, ModelPool.owner_user_id == owner_user_id)
            .with_for_update()
        )
        if pool is None:
            raise CatalogReferenceError("Model pool not found")
        if pool.purpose == OFFLINE_POOL_PURPOSE:
            raise CatalogConflictError("This pool belongs to an offline pipeline; delete the pipeline instead.")
        application_id = await self.session.scalar(
            select(ApplicationGatewayConfig.application_id)
            .where(ApplicationGatewayConfig.model_pool_id == pool.id)
            .limit(1)
        )
        if application_id is not None:
            raise CatalogConflictError(
                "This model pool is used by an Application. Change that Application's model pool before deleting it."
            )

        try:
            await self.session.execute(delete(ModelPoolMember).where(ModelPoolMember.pool_id == pool.id))
            await AuditRepository(self.session).add_event(
                actor_user_id=owner_user_id,
                action="model_pool.deleted",
                resource_type="model_pool",
                resource_id=pool.id,
            )
            await self.session.delete(pool)
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

    async def create_policy(self, payload: PolicyCreate) -> PolicyRead:
        if await self.repository.policy_by_name(payload.name):
            raise CatalogConflictError("Routing policy name already exists")
        policy = RoutingPolicy(
            name=payload.name,
            match_conditions_json=payload.match_conditions,
            guardrails_json=payload.guardrails,
            fallback_chain_json=payload.fallback_chain,
            priority=payload.priority,
            status=payload.status,
        )
        self.session.add(policy)
        try:
            await self.session.flush()
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise CatalogConflictError("Routing policy name already exists") from exc
        await self.session.refresh(policy)
        return self.policy_read(policy)

    async def list_policies(self) -> list[PolicyRead]:
        return [self.policy_read(item) for item in await self.repository.list_policies()]

    async def get_policy(self, policy_id: str) -> PolicyRead:
        policy = await self.repository.policy(policy_id)
        if policy is None:
            raise CatalogReferenceError("Routing policy not found")
        return self.policy_read(policy)

    @staticmethod
    def validate_member_input(payload: ModelPoolCreate) -> None:
        model_ids = [member.model_id for member in payload.members]
        if len(model_ids) != len(set(model_ids)):
            raise CatalogConflictError("A model cannot appear twice in a pool")

    @staticmethod
    def provider_read(provider: Provider) -> ProviderRead:
        return ProviderRead(
            id=provider.id,
            name=provider.name,
            display_name=provider.display_name,
            kind=provider.kind,
            purpose="target",
            status=provider.status,
            base_url=provider.base_url,
            created_at=provider.created_at,
            updated_at=provider.updated_at,
        )

    @staticmethod
    def model_read(model: Model) -> ModelRead:
        return ModelRead(
            id=model.id,
            provider_id=model.provider_id,
            name=model.name,
            display_name=model.display_name,
            status=model.status,
            capabilities=model.capabilities_json or [],
            quality_tier=model.quality_tier,
            context_window=model.context_window,
            max_output_tokens=model.max_output_tokens,
            feature_metadata=model.feature_metadata_json,
            target_latency_ms=model.target_latency_ms,
            input_price_per_million=model.input_price_per_million,
            output_price_per_million=model.output_price_per_million,
            pricing_version=model.pricing_version,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def pool_read(self, pool: ModelPool) -> ModelPoolRead:
        members = await self.repository.pool_members(pool.id)
        rule_members = []
        for member in members:
            model = await self.repository.model(member.model_id)
            if model is not None:
                provider = await self.session.get(Provider, model.provider_id)
                rule_members.append(pool_rules.PoolMember(member.role, model, provider))
        return ModelPoolRead(
            id=pool.id,
            name=pool.name,
            purpose=pool.purpose,
            status=pool.status,
            owner_user_id=pool.owner_user_id,
            members=[ModelPoolMemberRead.model_validate(member, from_attributes=True) for member in members],
            warnings=pool_rules.warnings(rule_members, await self._callable_target_provider_ids()),
            created_at=pool.created_at,
            updated_at=pool.updated_at,
        )

    @staticmethod
    def policy_read(policy: RoutingPolicy) -> PolicyRead:
        return PolicyRead(
            id=policy.id,
            name=policy.name,
            match_conditions=policy.match_conditions_json or {},
            guardrails=policy.guardrails_json or {},
            fallback_chain=policy.fallback_chain_json or [],
            priority=policy.priority,
            status=policy.status,
            created_at=policy.created_at,
            updated_at=policy.updated_at,
        )
