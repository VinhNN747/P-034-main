from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.core.pricing import ModelPricing, PricingRegistry, pricing_rules_from_metadata
from src.shared.dtos.gateway import PipelineResult, QualityPriority, RequestConstraints
from src.shared.models.gateway import ApplicationGatewayConfig, Model, Provider
from src.shared.models.runtime import (
    AggregationResultRecord,
    GatewayRequestRecord,
    GatewayTaskRecord,
    ModelExecutionRecord,
    StageTraceRecord,
)
from src.shared.repositories.routing_catalog import visible_to
from src.shared.services.gateway_persistence_service import GatewayPersistenceService


class GatewayRuntimeService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.persistence = GatewayPersistenceService(session)

    @staticmethod
    def apply_config_limits(
        constraints: RequestConstraints,
        config: ApplicationGatewayConfig | None,
    ) -> RequestConstraints:
        if config is None:
            return constraints
        updates: dict[str, Any] = {}
        max_output = config.max_output_tokens
        if max_output > 0:
            updates["max_output_tokens"] = min(constraints.max_output_tokens or max_output, max_output)
        if config.budget_ceiling is not None:
            updates["max_cost_usd"] = min(
                constraints.max_cost_usd or config.budget_ceiling,
                config.budget_ceiling,
            )
        if config.max_latency_ms is not None:
            updates["max_latency_ms"] = min(
                constraints.max_latency_ms or config.max_latency_ms,
                config.max_latency_ms,
            )
        if (
            constraints.quality_priority == QualityPriority.BALANCED
            and config.routing_objective in {item.value for item in QualityPriority}
            and "quality_priority" not in constraints.model_fields_set
        ):
            updates["quality_priority"] = QualityPriority(config.routing_objective)
        return constraints.model_copy(update=updates)

    async def model_runtime_catalog(
        self, owner_user_id: str | None = None
    ) -> tuple[PricingRegistry, list[tuple[Model, Provider]]]:
        """Load active runtime model metadata from the database.

        The returned aliases let deterministic routes resolve a model name to
        its provider adapter without relying on a filesystem catalog. Only the
        shared catalog and ``owner_user_id``'s own endpoints are loaded, so one
        customer's model names can never shadow another's or the system's.
        """
        rows = await self.session.execute(
            select(Model, Provider)
            .join(Provider, Provider.id == Model.provider_id)
            .where(Model.status == "active", Provider.status == "active", visible_to(owner_user_id))
        )
        pricing_entries: list[ModelPricing] = []
        catalog_rows: list[tuple[Model, Provider]] = []
        for model, provider in rows.all():
            if provider.purpose != "target":
                continue
            entry_values = {
                "provider": provider.kind,
                "input_price_per_million": model.input_price_per_million,
                "output_price_per_million": model.output_price_per_million,
                "pricing_version": model.pricing_version,
                "capabilities": set(model.capabilities_json or []),
                "estimated": True,
                "pricing_rules": pricing_rules_from_metadata(model.feature_metadata_json),
            }
            pricing_entries.append(ModelPricing(model_id=model.name, **entry_values))
            pricing_entries.append(ModelPricing(model_id=model.id, **entry_values))
            catalog_rows.append((model, provider))
        return PricingRegistry(pricing_entries), catalog_rows

    async def normalize_result(self, result: PipelineResult, source: str) -> dict[str, Any]:
        executions = result.execution_results or []
        successful = [item for item in executions if item.status == "success"]
        output = result.aggregation.final_answer if result.aggregation else None
        if output is None and successful:
            output = successful[0].result
        status = "failed"
        if result.validation.accepted and result.aggregation is not None:
            status = {
                "complete": "completed",
                "partial": "partial",
                "failed": "failed",
            }[result.aggregation.status.value]
        fallback = next((item for item in executions if item.fallback), None)
        identities = await self._model_identities({item.executed_model for item in successful if item.executed_model})
        models_used = list(dict.fromkeys(
            identities.get(item.executed_model, (item.executed_model, item.provider))[0]
            for item in successful
            if item.executed_model
        ))
        providers_used = list(dict.fromkeys(
            provider_name
            for item in successful
            if item.executed_model
            for provider_name in [identities.get(item.executed_model, (item.executed_model, item.provider))[1]]
            if provider_name
        ))
        input_tokens = sum(item.input_tokens for item in executions)
        output_tokens = sum(item.output_tokens for item in executions)
        return {
            "request_id": result.request.request_id,
            "application_id": result.request.application_id,
            "source": source,
            "status": status,
            "output": output,
            "provider": providers_used[0] if len(providers_used) == 1 else " + ".join(providers_used) or None,
            "model": models_used[0] if len(models_used) == 1 else " + ".join(models_used) or None,
            "providers_used": providers_used,
            "models_used": models_used,
            "latency_ms": round(sum(trace.latency_ms for trace in result.traces), 3),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost": result.costs.total_cost,
            "fallback_used": bool(fallback),
            "fallback_reason": fallback.failure_reason if fallback else None,
            "trace_count": len(result.traces),
        }

    async def request_detail(
        self,
        record: GatewayRequestRecord,
        *,
        application_name: str | None = None,
        application_deleted: bool = False,
        tasks: list[GatewayTaskRecord],
        aggregation: AggregationResultRecord | None,
        traces: list[StageTraceRecord],
        executions: list[ModelExecutionRecord],
    ) -> dict[str, Any]:
        input_tokens = sum(item.input_tokens for item in executions)
        output_tokens = sum(item.output_tokens for item in executions)
        successful = [item for item in executions if item.status == "success"]
        fallback = next((item for item in executions if item.fallback), None)
        output = aggregation.final_answer if aggregation else None
        model_ids = {item.model for item in executions if item.model}
        identities = await self._model_identities(model_ids)
        models_used = list(dict.fromkeys(
            identities.get(item.model, (item.model, item.provider))[0]
            for item in successful
            if item.model
        ))
        providers_used = list(dict.fromkeys(
            provider_name
            for item in successful
            if item.model
            for provider_name in [identities.get(item.model, (item.model, item.provider))[1]]
            if provider_name
        ))
        execution_items = []
        for execution in executions:
            item = self._model_item(execution) or {}
            model_name, provider_name = identities.get(
                execution.model,
                (execution.model, execution.provider),
            )
            item["model_name"] = model_name
            item["provider_name"] = provider_name
            execution_items.append(item)
        return {
            "request_id": record.request_id,
            "application_id": record.application_id,
            "application_name": application_name,
            "application_deleted": application_deleted,
            "source": record.source,
            "status": record.status,
            "aggregation_status": record.aggregation_status,
            "output": output,
            "provider": providers_used[0] if len(providers_used) == 1 else " + ".join(providers_used) or None,
            "model": models_used[0] if len(models_used) == 1 else " + ".join(models_used) or None,
            "providers_used": providers_used,
            "models_used": models_used,
            "latency_ms": record.total_latency_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost": record.total_cost,
            "fallback_used": bool(fallback),
            "fallback_reason": fallback.failure_reason if fallback else None,
            "trace_count": len(traces),
            "prompt": record.prompt,
            "constraints": record.constraints_json,
            "response": record.response_json,
            "tasks": [self._model_item(item) for item in tasks],
            "traces": [self._model_item(item) for item in traces],
            "executions": execution_items,
            "aggregation": self._model_item(aggregation),
            "started_at": record.started_at,
            "finished_at": record.finished_at,
        }

    async def config(self, application_id: str) -> ApplicationGatewayConfig | None:
        return await self.session.scalar(
            select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == application_id)
        )

    async def _model_identity(self, model_id: str | None) -> tuple[str | None, str | None]:
        if not model_id:
            return None, None
        model = await self.session.get(Model, model_id)
        if model is None:
            return model_id, "offline"
        provider = await self.session.get(Provider, model.provider_id)
        return model.name, provider.name if provider else None

    async def _model_identities(self, model_ids: set[str]) -> dict[str, tuple[str, str | None]]:
        if not model_ids:
            return {}
        rows = await self.session.execute(
            select(Model.id, Model.name, Provider.name)
            .join(Provider, Provider.id == Model.provider_id)
            .where(Model.id.in_(model_ids))
        )
        identities = {
            model_id: (model_name, provider_label)
            for model_id, model_name, provider_label in rows.all()
        }
        for model_id in model_ids:
            identities.setdefault(model_id, (model_id, None))
        return identities


    @staticmethod
    def _model_item(item: Any) -> dict[str, Any] | None:
        if item is None:
            return None
        return {key: value for key, value in vars(item).items() if not key.startswith("_")}


__all__ = ["GatewayRuntimeService"]
