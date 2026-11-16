from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.agents.routing.base import BaseRouter, RouteDecision
from src.backend_ai.agents.routing.router import ExecutionConfig
from src.backend_ai.dtos.gateway_runtime import GatewayRuntimeInput
from src.backend_ai.gateway.factory import (
    GatewayConfigurationError,
    build_file_limits,
    build_orchestrator,
    build_pipeline,
    build_shadow_tier_policy,
    build_tier_policy,
    ensure_strategy_available,
    get_file_storage,
)
from src.backend_ai.gateway.files.service import FileServiceError, GatewayFileService
from src.backend_ai.gateway.model_alias import template_for_alias
from src.backend_ai.gateway.multimodal.attachments import redact_inline_media, referenced_file_ids
from src.backend_ai.gateway.pipeline_profile import pipeline_profile, settings_for_application
from src.backend_ai.gateway.routing.session_affinity import SessionAffinity
from src.backend_ai.latency.context import LatencyRequestContext, latency_request_scope
from src.backend_ai.latency.contracts import ModelKey
from src.backend_ai.services.gateway_routing_service import (
    DatabaseRoutingService,
    DatabaseRoutingSnapshot,
    RoutingError,
    supports_text_generation,
)
from src.backend_ai.services.internal_provider_clients import InternalProviderClients, build_internal_clients
from src.backend_ai.services.provider_client_builder import ProviderClientBuilder
from src.shared.config import Settings, get_settings
from src.shared.core.pricing import ModelPricing, PricingRegistry, pricing_rules_from_metadata
from src.shared.dtos.gateway import GatewayRequest
from src.shared.dtos.gateway import RequestConstraints as InternalConstraints
from src.shared.dtos.runtime_api import RequestConstraints, RuntimeAPIError, RuntimeRequestInput
from src.shared.models.core import Application
from src.shared.models.gateway import (
    ApplicationGatewayConfig,
    ModelPool,
    RoutingPolicy,
)
from src.shared.models.runtime import GatewayRequestRecord
from src.shared.repositories.routing_catalog import RoutingSet, is_system_pool, routing_set
from src.shared.services.application_budget_service import ApplicationBudgetService, BudgetExceededError
from src.shared.services.credential_crypto import CredentialConfigurationError, ensure_credential_crypto
from src.shared.services.gateway_persistence_service import GatewayPersistenceService
from src.shared.services.gateway_runtime_service import GatewayRuntimeService
from src.shared.services.provider_credential_resolver import ProviderCredentialResolver

_STRATEGIES = {
    "understander": {"rule", "llm", "jev", "classifier"},
    "decomposer": {"none", "deterministic", "llm"},
    "router": {"deterministic", "database", "jev"},
    "aggregator": {"auto", "none", "deterministic", "llm"},
}
_PUBLIC_STRATEGIES = {
    "understander": {"rule", "llm", "jev", "classifier"},
    "decomposer": {"none", "deterministic", "llm"},
    "aggregator": {"auto", "none", "deterministic", "llm"},
}


class ApplicationDatabaseRouter(BaseRouter):
    name = "database"

    def __init__(
        self,
        service: DatabaseRoutingService,
        constraints: InternalConstraints | None = None,
        *,
        use_effective_constraints: bool = False,
    ):
        self.service = service
        self.constraints = constraints or InternalConstraints()
        self.use_effective_constraints = use_effective_constraints

    async def aroute(self, subtask, upstream_results=None, **kwargs) -> RouteDecision:
        constraints = (
            kwargs.get("constraints", self.constraints) if self.use_effective_constraints else self.constraints
        )
        return await self.service.route(subtask, constraints, latency_features=kwargs.get("latency_features"))


@dataclass(frozen=True)
class PreparedGatewayExecution:
    application_id: str
    config: ApplicationGatewayConfig
    pool: ModelPool | None
    policy: RoutingPolicy | None
    routing_snapshot: DatabaseRoutingSnapshot
    snapshot_json: dict[str, Any]
    pricing: PricingRegistry
    providers: dict
    available_provider_ids: set[str]
    model_identities: dict[str, tuple[str, str, str]]
    model_bindings: dict[str, ModelKey]
    application_policy: InternalConstraints
    request_constraints: InternalConstraints
    understander: str
    decomposer: str
    aggregator: str
    requested_router: str
    settings: Settings
    internal_clients: InternalProviderClients
    warning_codes: tuple[str, ...]
    resolved_files: dict[str, Any] | None = None
    """Files API uploads referenced by ``file_id``, loaded once in prepare."""


class GatewayExecutionService:
    """Use-case service shared by native application-key and legacy routes."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None, latency_runtime=None):
        self.session = session
        self.settings = settings or get_settings()
        self.latency_runtime = latency_runtime
        self.openai_payload = None
        # Auto-routing set (own pool, default pool or task template), read with the snapshot.
        self._routing_set: RoutingSet | None = None

    async def prepare_public(
        self,
        principal,
        payload: RuntimeRequestInput,
    ) -> PreparedGatewayExecution:
        try:
            alias = (getattr(payload, "metadata", None) or {}).get("model_alias")
            application, config, pool, policy, rows = await self._read_snapshot(
                principal.application_id,
                template=template_for_alias(alias),
            )
        except SQLAlchemyError as exc:
            await self.session.rollback()
            raise RuntimeAPIError(
                503,
                "RUNTIME_STORAGE_UNAVAILABLE",
                "Gateway configuration could not be loaded.",
                retryable=True,
            ) from exc

        if application is None or application.status != "active":
            raise RuntimeAPIError(403, "APPLICATION_INACTIVE", "The application is inactive.")
        if config is None:
            raise RuntimeAPIError(503, "GATEWAY_NOT_CONFIGURED", "The application gateway is not configured.")
        if config.model_pool_id and (
            pool is None
            or pool.status != "active"
            or (pool.owner_user_id != application.owner_user_id and not is_system_pool(pool))
        ):
            raise RuntimeAPIError(503, "GATEWAY_NOT_CONFIGURED", "The configured model pool is inactive.")
        if config.routing_policy_id and (policy is None or policy.status != "active"):
            raise RuntimeAPIError(503, "GATEWAY_NOT_CONFIGURED", "The application routing policy is unavailable.")

        # From here on every stage reads this request's own settings: an offline pipeline runs offline next to
        # online applications in the same process (pipeline_profile.settings_for_application).
        self.settings = settings_for_application(self.settings, application, config)
        normalized = RuntimeRequestInput.model_validate(payload)
        if normalized.comparison is not None and application.pipeline == "offline":
            raise RuntimeAPIError(
                422,
                "COMPARISON_UNAVAILABLE",
                "Model comparison is not available for offline pipelines.",
            )
        requested = {
            "understander": config.default_understander,
            "decomposer": config.default_decomposer,
            "aggregator": config.default_aggregator,
        }
        for field, strategy in requested.items():
            if strategy not in _PUBLIC_STRATEGIES[field]:
                raise RuntimeAPIError(
                    503,
                    "STRATEGY_UNAVAILABLE",
                    f"The configured {field} strategy is not available to the public runtime.",
                    details={"strategy": strategy},
                )
        # The pipeline (online / offline) decides what each stage runs; offline maps jev/llm onto local stages.
        requested, substituted = pipeline_profile(self.settings).resolve(requested)

        client_constraints = self._internal_constraints(normalized.constraints)
        candidate_rows = []
        for member, model, provider in rows:
            if (
                model.status == "active"
                and provider.status == "active"
                and provider.purpose == "target"
                and supports_text_generation(model)
            ):
                candidate_rows.append((member, model, provider))
        if not candidate_rows:
            raise RuntimeAPIError(422, "NO_ELIGIBLE_MODEL", "The routing candidate set has no active models.")
        try:
            crypto = ensure_credential_crypto(self.settings)
        except CredentialConfigurationError as exc:
            raise RuntimeAPIError(
                503,
                "CREDENTIAL_ENCRYPTION_UNAVAILABLE",
                "Provider credentials are not available for this runtime.",
            ) from exc
        client_builder = ProviderClientBuilder(
            ProviderCredentialResolver(self.session, crypto), self.settings, self.latency_runtime
        )
        providers = await client_builder.build_for_models([(model, provider) for _, model, provider in candidate_rows])
        available_provider_ids = client_builder.available_provider_ids
        if self.session.in_transaction():
            await self.session.commit()
        active_rows = [row for row in candidate_rows if row[2].id in available_provider_ids]
        if not active_rows:
            if self.settings.gateway_execution_mode == "live":
                raise RuntimeAPIError(
                    503,
                    "GATEWAY_NOT_CONFIGURED",
                    "No configured provider connection is available for this application candidate set.",
                )
            raise RuntimeAPIError(422, "NO_ELIGIBLE_MODEL", "No provider adapter is available in the candidate set.")
        allowed_models: set[str] = set()
        allowed_providers: set[str] = set()
        pricing_entries: list[ModelPricing] = []
        identities: dict[str, tuple[str, str, str]] = {}
        for member, model, provider in rows:
            if provider.id not in available_provider_ids or model.status != "active" or provider.status != "active":
                continue
            allowed_models.update((model.id, model.name))
            allowed_providers.update((provider.id, provider.name, provider.kind))
            identity = (model.id, provider.name, provider.kind)
            identities[model.id] = identity
            identities[model.name] = identity
            entry_values = {
                "provider": provider.kind,
                "input_price_per_million": model.input_price_per_million,
                "output_price_per_million": model.output_price_per_million,
                "pricing_version": model.pricing_version,
                "capabilities": set(model.capabilities_json or []),
                "estimated": True,
                "pricing_rules": pricing_rules_from_metadata(model.feature_metadata_json),
            }
            pricing_entries.extend(
                [ModelPricing(model_id=model.id, **entry_values), ModelPricing(model_id=model.name, **entry_values)]
            )
        try:
            self._ensure_unambiguous_model_pins(active_rows, client_constraints.allowed_models)
        except ValueError as exc:
            raise RuntimeAPIError(422, "AMBIGUOUS_MODEL", str(exc)) from exc
        if normalized.comparison is not None:
            active_model_ids = {model.id for _, model, _ in candidate_rows}
            requested_references = set(normalized.comparison.reference_model_ids)
            if not requested_references.issubset(active_model_ids):
                raise RuntimeAPIError(
                    422,
                    "INVALID_COMPARISON_MODEL",
                    "Every comparison model must be an active member of the application pool.",
                )
            if (
                normalized.comparison.evaluator_model_id is not None
                and normalized.comparison.evaluator_model_id not in active_model_ids
            ):
                raise RuntimeAPIError(
                    422,
                    "INVALID_COMPARISON_EVALUATOR",
                    "The evaluator model must be an active member of the application pool.",
                )

        requested_models = client_constraints.allowed_models
        requested_providers = client_constraints.allowed_providers
        if requested_models is not None:
            allowed_models.intersection_update(requested_models)
        if requested_providers is not None:
            allowed_providers.intersection_update(requested_providers)
        if not allowed_models or not allowed_providers:
            raise RuntimeAPIError(
                422,
                "NO_ELIGIBLE_MODEL",
                "No model in the routing candidate set satisfies the requested allow-list.",
            )

        app_values: dict[str, Any] = {
            "allowed_models": allowed_models,
            "allowed_providers": allowed_providers,
            "max_output_tokens": min(max(1, config.max_output_tokens), 32_768),
        }
        if config.max_latency_ms is not None:
            app_values["max_latency_ms"] = config.max_latency_ms
        if config.budget_ceiling is not None:
            app_values["max_cost_usd"] = config.budget_ceiling
        if normalized.constraints.quality_priority is None:
            app_values["quality_priority"] = config.routing_objective
        application_policy = InternalConstraints.model_validate(app_values)
        internal_request_constraints = self._internal_constraints(normalized.constraints)
        request = GatewayRequest(
            request_id=str(uuid4()),
            application_id=principal.application_id,
            prompt=normalized.prompt,
            messages=normalized.messages,
            context=normalized.context,
            constraints=internal_request_constraints,
            metadata={"source": "api", "client_metadata": normalized.metadata},
        )
        try:
            await self._resolve_files(request)
        except FileServiceError as exc:
            raise RuntimeAPIError(
                exc.status_code,
                exc.code.upper(),
                exc.message,
                details={"param": exc.param} if exc.param else {},
            ) from exc
        resolver = ProviderCredentialResolver(self.session, crypto)
        internal_clients = await build_internal_clients(
            resolver,
            self.settings,
            need_llm=any(strategy == "llm" for strategy in requested.values()),
            need_jev=requested["understander"] == "jev",
            need_ocr=request.has_attachments(),
        )
        try:
            for strategy in requested.values():
                if strategy in {"llm", "jev"}:
                    ensure_strategy_available(strategy, self.settings, internal_clients)
        except GatewayConfigurationError as exc:
            raise RuntimeAPIError(
                503,
                "STRATEGY_UNAVAILABLE",
                "The configured gateway strategy is unavailable.",
            ) from exc
        if self.session.in_transaction():
            await self.session.commit()
        pipeline = build_pipeline(
            settings=self.settings,
            pricing=PricingRegistry(pricing_entries),
            internal_clients=internal_clients,
        )
        if str(normalized.metadata.get("api_surface", "")).startswith("openai"):
            from src.backend_ai.services.openai_media_policy import preflight_media

            preflight_media(request, pipeline.attachment_analyzer)
        validation = pipeline.request_validator.validate(request)
        if not validation.accepted:
            raise RuntimeAPIError(
                422,
                "REQUEST_REJECTED",
                "The request did not pass gateway validation.",
                details={"issues": [{"code": issue.code, "field": issue.field} for issue in validation.errors]},
            )
        effective = pipeline.constraint_resolver.resolve(
            request,
            application_policy=application_policy,
        )
        if effective.conflicts:
            raise RuntimeAPIError(422, "NO_ELIGIBLE_MODEL", "No model satisfies the application policies.")

        if normalized.comparison is not None:
            available_model_ids = {model.id for _, model, _ in active_rows}
            comparison_ids = set(normalized.comparison.reference_model_ids)
            if normalized.comparison.quality_evaluation and normalized.comparison.evaluator_model_id:
                comparison_ids.add(normalized.comparison.evaluator_model_id)
            if not comparison_ids.issubset(available_model_ids):
                raise RuntimeAPIError(
                    503,
                    "COMPARISON_PROVIDER_UNAVAILABLE",
                    "A provider adapter for at least one comparison model is unavailable.",
                )
        for kind, value in (
            ("model", effective.constraints.allowed_models),
            ("provider", effective.constraints.allowed_providers),
        ):
            if value is not None and not value:
                raise RuntimeAPIError(422, "NO_ELIGIBLE_MODEL", f"No eligible {kind} remains after policy resolution.")

        # The operation claim intentionally starts a serialized transaction and
        # rolls back any transaction opened while preparing the request. Keep
        # the prepared catalog detached from that session boundary so the
        # execution path never triggers async lazy loads after the claim.
        config_snapshot = deepcopy(config)
        pool_snapshot = deepcopy(pool) if pool is not None else None
        policy_snapshot = deepcopy(policy) if policy is not None else None
        active_rows_snapshot = tuple(
            (deepcopy(member) if member is not None else None, deepcopy(model), deepcopy(provider))
            for member, model, provider in active_rows
        )
        routing_snapshot = DatabaseRoutingSnapshot(
            config=config_snapshot,
            policy=policy_snapshot,
            candidates=active_rows_snapshot,
            routing=deepcopy(self._routing_set) if self._routing_set is not None else None,
        )
        config_data = {
            "id": config.id,
            "application_id": config.application_id,
            "version": config.version,
            "model_pool_id": config.model_pool_id,
            "routing_policy_id": config.routing_policy_id,
            "routing_objective": config.routing_objective,
            "budget_ceiling": config.budget_ceiling,
            "warning_threshold": config.warning_threshold,
            "budget_action": config.budget_action,
            "max_output_tokens": config.max_output_tokens,
            "max_latency_ms": config.max_latency_ms,
            "timeout_seconds": config.timeout_seconds,
            "retry_limit": config.retry_limit,
            "fallback_enabled": config.fallback_enabled,
            "fallback_model_id": config.fallback_model_id,
            "fallback_model_ids": list(config.fallback_model_ids_json or []),
            "default_understander": config.default_understander,
            "default_decomposer": config.default_decomposer,
            "default_router": config.default_router,
            "default_aggregator": config.default_aggregator,
            "context_window": config.context_window,
        }
        snapshot_json = {
            "config": config_data,
            "pool": {
                "id": pool.id,
                "name": pool.name,
                "purpose": pool.purpose,
                "status": pool.status,
                "owner_user_id": pool.owner_user_id,
            }
            if pool
            else None,
            "policy": {
                "id": policy.id,
                "name": policy.name,
                "priority": policy.priority,
                "status": policy.status,
                "match_conditions": policy.match_conditions_json or {},
                "guardrails": policy.guardrails_json or {},
                "quality_scores": (policy.guardrails_json or {}).get("quality_scores", {}),
                "fallback_chain": list(policy.fallback_chain_json or []),
            }
            if policy
            else None,
            "models": [
                {
                    "pool_member_id": member.id if member else None,
                    "pool_id": member.pool_id if member else None,
                    "id": model.id,
                    "name": model.name,
                    "status": model.status,
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "provider_kind": provider.kind,
                    "provider_status": provider.status,
                    "capabilities": list(model.capabilities_json or []),
                    "context_window": model.context_window,
                    "target_latency_ms": model.target_latency_ms,
                    "input_price_per_million": model.input_price_per_million,
                    "output_price_per_million": model.output_price_per_million,
                    "pricing_version": model.pricing_version,
                    "pricing_rules": (
                        pricing_rules_from_metadata(model.feature_metadata_json).model_dump(mode="json")
                        if pricing_rules_from_metadata(model.feature_metadata_json) is not None
                        else None
                    ),
                    "quality_tier": model.quality_tier,
                    "health_status": model.health_status,
                }
                for member, model, provider in active_rows
            ],
            "execution_mode": self.settings.gateway_execution_mode,
            "pipeline": pipeline_profile(self.settings).name,
            "strategies": {
                **requested,
                "router_requested": config.default_router,
                "router_effective": "database",
                **({"substituted": substituted} if substituted else {}),
            },
        }
        warnings = ("ROUTER_FORCED_DATABASE",) if config.default_router != "database" else ()
        if substituted:
            warnings = (*warnings, "PIPELINE_STRATEGY_SUBSTITUTED")
        if config.budget_ceiling is not None:
            warnings = (*warnings, "BUDGET_LIMIT_ADVISORY")
        return PreparedGatewayExecution(
            application_id=application.id,
            config=config_snapshot,
            pool=pool_snapshot,
            policy=policy_snapshot,
            routing_snapshot=routing_snapshot,
            snapshot_json=snapshot_json,
            pricing=PricingRegistry(pricing_entries),
            providers=providers,
            available_provider_ids=available_provider_ids,
            model_identities=identities,
            model_bindings=client_builder.model_bindings,
            application_policy=application_policy,
            request_constraints=internal_request_constraints,
            understander=requested["understander"],
            decomposer=requested["decomposer"],
            aggregator=requested["aggregator"],
            requested_router=config.default_router,
            settings=self.settings,
            internal_clients=internal_clients,
            warning_codes=warnings,
            resolved_files=request.resolved_files(),
        )

    def _latency_request_context(
        self,
        request_id: str,
        application_id: str,
        source: str,
        execution_mode: str,
        bindings: dict[str, ModelKey],
    ):
        runtime = self.latency_runtime
        if runtime is None or execution_mode != "live" or source in {"comparison", "internal", "probe", "offline"}:
            return None
        from src.backend_ai.latency.predictor import LatencyResolver

        artifact = runtime.model_store.snapshot()
        history = runtime.history_cache.snapshot()
        return LatencyRequestContext(
            request_id=request_id,
            source=source,
            mode=runtime.config.mode,
            artifact=artifact,
            history=history,
            collector=runtime.collector,
            bindings=bindings,
            application_id=application_id,
            resolver=LatencyResolver(artifact, history, runtime.config),
        )

    async def run_public(
        self,
        principal,
        payload: RuntimeRequestInput,
        prepared: PreparedGatewayExecution,
        request_id: str,
        operation_started_at,
        deadline_at: float,
    ):
        source = "openai_sdk" if self.openai_payload is not None else "api"
        latency_context = self._latency_request_context(
            request_id,
            prepared.application_id,
            source,
            prepared.settings.gateway_execution_mode,
            prepared.model_bindings,
        )
        if latency_context is None:
            return await self._run_public_impl(
                principal, payload, prepared, request_id, operation_started_at, deadline_at
            )
        with latency_request_scope(latency_context):
            return await self._run_public_impl(
                principal, payload, prepared, request_id, operation_started_at, deadline_at
            )

    async def _run_public_impl(
        self,
        principal,
        payload: RuntimeRequestInput,
        prepared: PreparedGatewayExecution,
        request_id: str,
        operation_started_at,
        deadline_at: float,
    ):
        request = GatewayRequest(
            request_id=request_id,
            application_id=principal.application_id,
            prompt=payload.prompt,
            messages=payload.messages,
            context=payload.context,
            constraints=prepared.request_constraints,
            metadata={
                "source": "api",
                "understander": prepared.understander,
                "decomposer": prepared.decomposer,
                "router": "database",
                "router_requested": prepared.requested_router,
                "aggregator": prepared.aggregator,
                "client_metadata": payload.metadata,
                "openai_hard_completion_token_limit": payload.metadata.get("openai_hard_completion_token_limit"),
                "config_version": prepared.config.version,
                "execution_mode": prepared.settings.gateway_execution_mode,
                # Latency-first callers (e.g. OpenAI model "auto:fast") skip
                # task decomposition: every extra subtask is another round trip.
                "disable_decomposition": payload.constraints.quality_priority == "latency_first"
                or self.openai_payload is not None,
            },
        )
        request.set_resolved_files(prepared.resolved_files or {})
        pipeline = build_pipeline(
            settings=prepared.settings,
            pricing=prepared.pricing,
            internal_clients=prepared.internal_clients,
        )
        pipeline.application_policies[prepared.application_id] = prepared.application_policy
        affinity = SessionAffinity.for_request(
            request,
            enabled=prepared.settings.gateway_session_affinity_enabled,
            ttl_seconds=prepared.settings.gateway_session_affinity_ttl_seconds,
        )
        database_router = ApplicationDatabaseRouter(
            DatabaseRoutingService(
                self.session,
                prepared.application_id,
                available_provider_ids=prepared.available_provider_ids,
                snapshot=prepared.routing_snapshot,
                tier_policy=build_tier_policy(prepared.settings),
                shadow_policy=build_shadow_tier_policy(prepared.settings),
                affinity=affinity,
            ),
            prepared.request_constraints,
            use_effective_constraints=True,
        )
        if self.openai_payload is not None:
            from src.backend_ai.services.openai_message_execution import execute_openai_messages

            async with asyncio.timeout(max(0.001, deadline_at - time.monotonic())):
                result = await pipeline.run(request, understander=prepared.understander, decomposer="none")
                from src.backend_ai.services.runtime_stream_service import current_stream

                if stream := current_stream():
                    stream.partial_result = result.model_copy(deep=True)
                if not result.validation.accepted or result.semantic_analysis is None:
                    return result
                result = await execute_openai_messages(
                    request, result, self.openai_payload, prepared, database_router, deadline_at
                )
                if affinity is not None:
                    affinity.record_outcome(result.execution_results)
                return result
        # A quality objective is a configurable application default. Preserve a
        # caller's explicit balanced value by omitting the app default in that case.
        orchestrator = build_orchestrator(
            settings=prepared.settings,
            pipeline=pipeline,
            internal_clients=prepared.internal_clients,
            database_router=database_router,
            providers=prepared.providers,
            pricing=prepared.pricing,
            persistence=None,
            execution_config=ExecutionConfig(
                timeout_seconds=min(
                    prepared.config.timeout_seconds,
                    prepared.settings.gateway_runtime_deadline_seconds,
                ),
                retry_limit=prepared.config.retry_limit,
                max_output_tokens=prepared.application_policy.max_output_tokens,
                deadline_at=deadline_at,
                generation_options_enabled=True,
            ),
        )
        async with asyncio.timeout(max(0.001, deadline_at - time.monotonic())):
            result = await orchestrator.run(
                request,
                understander=prepared.understander,
                decomposer=prepared.decomposer,
                router="database",
                aggregator=prepared.aggregator,
            )
        if affinity is not None:
            # Remember the model that actually answered this turn (fallback included).
            affinity.record_outcome(result.execution_results)
        return result

    async def run_legacy(self, payload: GatewayRuntimeInput, user, *, orchestrator_builder=None) -> dict[str, Any]:
        self._validate_legacy_strategies(payload)
        await self.session.rollback()
        from sqlalchemy import select

        application = await self.session.scalar(
            select(Application).where(
                Application.id == payload.application_id,
                Application.owner_user_id == user.id,
            )
        )
        if application is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Application not found")
        config = await self.session.scalar(
            select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == payload.application_id)
        )
        if config is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=503, detail="Gateway configuration not found")
        if application.status != "active":
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="Application is inactive")
        try:
            await ApplicationBudgetService(self.session).ensure_can_execute(payload.application_id)
        except BudgetExceededError as exc:
            raise HTTPException(
                status_code=429,
                detail={"code": exc.code, "message": str(exc), "budget": exc.snapshot.as_dict()},
            ) from exc
        # The playground runs the application's own pipeline, like its API key does.
        self.settings = settings_for_application(self.settings, application, config)
        effective_payload = payload.model_copy(
            update={
                "constraints": GatewayRuntimeService.apply_config_limits(payload.constraints, config),
                "router": "database",
                # Playground follows the application's understander unless it picks one.
                "understander": payload.understander or config.default_understander,
            }
        )
        effective_strategies, substituted = pipeline_profile(self.settings).resolve({
            stage: getattr(effective_payload, stage) for stage in ("understander", "decomposer", "aggregator")
        })
        effective_payload = effective_payload.model_copy(update=effective_strategies)
        request = effective_payload.to_gateway_request()
        request.metadata["execution_mode"] = self.settings.gateway_execution_mode
        request.metadata["pipeline"] = pipeline_profile(self.settings).name
        if substituted:
            request.metadata["strategies_substituted"] = substituted
        try:
            await self._resolve_files(request)
        except FileServiceError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        runtime_service = GatewayRuntimeService(self.session)
        pricing, model_rows = await runtime_service.model_runtime_catalog(application.owner_user_id)
        try:
            crypto = ensure_credential_crypto(self.settings)
        except CredentialConfigurationError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=503, detail="Provider credentials are not available.") from exc
        client_builder = ProviderClientBuilder(
            ProviderCredentialResolver(self.session, crypto), self.settings, self.latency_runtime
        )
        providers = await client_builder.build_for_models(model_rows)
        resolver = ProviderCredentialResolver(self.session, crypto)
        internal_clients = await build_internal_clients(
            resolver,
            self.settings,
            need_llm=any(
                value == "llm"
                for value in (effective_payload.understander, effective_payload.decomposer, effective_payload.aggregator)
            ),
            need_jev=effective_payload.understander == "jev" or effective_payload.router == "jev",
            need_ocr=request.has_attachments(),
            need_legacy_router=effective_payload.router == "jev",
        )
        try:
            for strategy in (effective_payload.understander, effective_payload.decomposer, effective_payload.aggregator):
                if strategy in {"llm", "jev"}:
                    ensure_strategy_available(strategy, self.settings, internal_clients)
            if effective_payload.router == "jev" and internal_clients.legacy_jev_router is None:
                raise GatewayConfigurationError("The legacy JEV router requires its internal service bindings.")
        except GatewayConfigurationError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=503, detail="The requested gateway strategy is unavailable.") from exc
        if self.session.in_transaction():
            await self.session.commit()
        try:
            self._ensure_unambiguous_model_pins(
                [
                    (None, model, provider)
                    for model, provider in model_rows
                    if provider.id in client_builder.available_provider_ids
                ],
                set(effective_payload.constraints.allowed_models or []),
            )
        except ValueError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=422, detail=str(exc)) from exc
        affinity = SessionAffinity.for_request(
            request,
            enabled=self.settings.gateway_session_affinity_enabled,
            ttl_seconds=self.settings.gateway_session_affinity_ttl_seconds,
        )
        database_router = ApplicationDatabaseRouter(
            DatabaseRoutingService(
                self.session,
                payload.application_id,
                available_provider_ids=client_builder.available_provider_ids,
                tier_policy=build_tier_policy(self.settings),
                shadow_policy=build_shadow_tier_policy(self.settings),
                affinity=affinity,
            ),
            effective_payload.constraints,
        )
        wall_started_at = datetime.now(UTC)
        wall_started = time.monotonic()
        try:
            orchestrator = (orchestrator_builder or build_orchestrator)(
                settings=self.settings,
                database_router=database_router,
                persistence=GatewayPersistenceService(self.session),
                providers=providers,
                internal_clients=internal_clients,
                pricing=pricing,
                execution_config=ExecutionConfig(
                    timeout_seconds=config.timeout_seconds if config else self.settings.gateway_routing_timeout_seconds,
                ),
            )
            latency_context = self._latency_request_context(
                request.request_id,
                payload.application_id,
                payload.source,
                self.settings.gateway_execution_mode,
                client_builder.model_bindings,
            )
            if latency_context is None:
                result = await orchestrator.run(
                    request,
                    understander=effective_payload.understander,
                    decomposer=effective_payload.decomposer,
                    router="database",
                    aggregator=effective_payload.aggregator,
                )
            else:
                with latency_request_scope(latency_context):
                    result = await orchestrator.run(
                        request,
                        understander=effective_payload.understander,
                        decomposer=effective_payload.decomposer,
                        router="database",
                        aggregator=effective_payload.aggregator,
                    )
            if affinity is not None:
                # Remember the model that actually answered this turn (fallback included).
                affinity.record_outcome(result.execution_results)
        except GatewayConfigurationError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RoutingError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail=str(exc)) from exc
        body = result.model_dump(mode="json")
        body["request_id"] = request.request_id
        # Never echo inline base64 images/files back to the caller.
        body["request"]["messages"] = [redact_inline_media(message) for message in request.messages or []]
        body.update(await runtime_service.normalize_result(result, effective_payload.source))
        wall_finished_at = datetime.now(UTC)
        wall_latency_ms = round((time.monotonic() - wall_started) * 1000, 3)
        body["latency_ms"] = wall_latency_ms
        persisted = await self.session.get(GatewayRequestRecord, request.request_id)
        if persisted is not None:
            persisted.total_latency_ms = wall_latency_ms
            persisted.started_at = wall_started_at
            persisted.finished_at = wall_finished_at
            await self.session.commit()
        return body

    @staticmethod
    def _ensure_unambiguous_model_pins(rows, requested_models: set[str] | None) -> None:
        if not requested_models:
            return
        models_by_name: dict[str, set[str]] = {}
        for _, model, _ in rows:
            models_by_name.setdefault(model.name, set()).add(model.id)
        ambiguous = sorted(name for name in requested_models if len(models_by_name.get(name, set())) > 1)
        if ambiguous:
            raise ValueError("The pinned model name is ambiguous; use a model ID or select a single connection.")

    async def _resolve_files(self, request: GatewayRequest) -> None:
        """Loads Files API uploads referenced by ``file_id`` (scoped to the
        request's application) onto the request; raises FileServiceError."""
        file_ids = referenced_file_ids(request)
        if not file_ids:
            return
        files = GatewayFileService(
            self.session,
            get_file_storage(self.settings),
            build_file_limits(self.settings),
            storage_resolver=lambda backend: get_file_storage(self.settings, backend=backend),
        )
        public = str(request.metadata.get("client_metadata", {}).get("api_surface", "")).startswith("openai")
        request.set_resolved_files(
            await files.resolve(
                request.application_id,
                file_ids,
                max_analysis_bytes=self.settings.gateway_attachment_max_bytes if public else None,
            )
        )
        # End the read transaction like _read_snapshot does: the operation
        # claim rolls back any open transaction, which would expire the
        # snapshot objects prepare_public hands to run_public.
        if self.session.in_transaction():
            await self.session.commit()

    async def _read_snapshot(self, application_id: str, *, template: str | None = None):
        bind = self.session.bind
        dialect = bind.dialect.name if bind is not None else ""
        if self.session.in_transaction():
            await self.session.rollback()
        if dialect == "sqlite":
            await self.session.execute(text("BEGIN IMMEDIATE"))
        else:
            await self.session.begin()
            if dialect == "postgresql":
                await self.session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        application = await self.session.get(Application, application_id)
        config = await self.session.scalar(
            select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == application_id)
        )
        pool = await self.session.get(ModelPool, config.model_pool_id) if config and config.model_pool_id else None
        policy = (
            await self.session.get(RoutingPolicy, config.routing_policy_id)
            if config and config.routing_policy_id
            else None
        )
        self._routing_set = await routing_set(self.session, config.model_pool_id, template=template) if config else None
        rows = self._routing_set.rows if self._routing_set else []
        await self.session.commit()
        return application, config, pool, policy, rows

    @staticmethod
    def _internal_constraints(constraints: RequestConstraints) -> InternalConstraints:
        values = constraints.model_dump(mode="python", exclude_unset=True, exclude_none=True)
        for field in ("allowed_models", "blocked_models", "allowed_providers", "blocked_providers"):
            if field in values:
                values[field] = set(values[field])
        return InternalConstraints.model_validate(values)

    @staticmethod
    def _validate_legacy_strategies(payload: GatewayRuntimeInput) -> None:
        from fastapi import HTTPException

        for field, choices in _STRATEGIES.items():
            value = getattr(payload, field)
            if value is None and field == "understander":
                continue  # resolved from the application's gateway config
            if value not in choices:
                raise HTTPException(status_code=400, detail=f"Unknown {field} '{value}'")
