from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.dtos.gateway import PipelineResult
from src.shared.models.runtime import (
    AggregationResultRecord,
    GatewayRequestRecord,
    GatewayTaskRecord,
    IncidentRecord,
    ModelExecutionRecord,
    RouteDecisionRecord,
    StageTraceRecord,
    UsageEventRecord,
)
from src.shared.repositories.gateway_persistence import GatewayPersistenceRepository
from src.shared.services.billing_service import BillingService
from src.shared.services.budget_alert_service import BudgetAlertService
from src.shared.services.media_redaction import redact_inline_media


class GatewayPersistenceService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = GatewayPersistenceRepository(session)

    async def save_run(
        self, result: PipelineResult, execution_results: list[Any] | None = None
    ) -> GatewayRequestRecord:
        record = await self.stage_run(result, execution_results)
        await self.session.commit()
        await self.session.refresh(record)
        try:
            await BudgetAlertService(self.session).notify_if_needed(result.request.application_id)
        except Exception:
            # Notifications are side effects and must not make a persisted
            # gateway request look failed to its caller.
            import logging

            logging.getLogger("uvicorn.error").exception(
                "Budget alert notification failed for %s", result.request.application_id
            )
        return record

    async def stage_run(
        self,
        result: PipelineResult,
        execution_results: list[Any] | None = None,
        *,
        application_api_key_id: str | None = None,
        config_version: int | None = None,
        started_at_override: datetime | None = None,
        finished_at_override: datetime | None = None,
        status_override: str | None = None,
    ) -> GatewayRequestRecord:
        request_id = result.request.request_id
        current = await self.repository.request(request_id)
        if current is not None:
            raise ValueError(f"Gateway request '{request_id}' already exists")

        execution_payloads = [self._dump(item) for item in (execution_results or result.execution_results or [])]
        traces = result.traces
        started_at = min((trace.started_at for trace in traces), default=datetime.now(UTC))
        finished_at = max((trace.finished_at for trace in traces), default=datetime.now(UTC))
        started_at = started_at_override or started_at
        finished_at = finished_at_override or finished_at
        aggregation = result.aggregation
        aggregation_status = aggregation.status.value if aggregation else None
        status = status_override or self._request_status(result)
        request_record = GatewayRequestRecord(
            request_id=request_id,
            application_id=result.request.application_id,
            application_api_key_id=application_api_key_id,
            config_version=config_version,
            prompt=result.request.prompt,
            messages_json=[redact_inline_media(message) for message in result.request.messages or []],
            constraints_json=self._dump(result.request.constraints),
            source=str(result.request.metadata.get("source", "unknown")),
            strategies_json={
                "understander": result.request.metadata.get("understander"),
                "decomposer": result.request.metadata.get("decomposer"),
                "router": result.request.metadata.get("router"),
                "aggregator": result.request.metadata.get("aggregator"),
            },
            response_json=self._dump(result),
            status=status,
            aggregation_status=aggregation_status,
            total_cost=result.costs.total_cost,
            total_latency_ms=round(sum(trace.latency_ms for trace in traces), 3),
            started_at=started_at,
            finished_at=finished_at,
        )
        self.session.add(request_record)
        try:
            # Persist the parent first. The runtime record models intentionally
            # don't expose ORM relationships, so a query-triggered autoflush
            # cannot otherwise infer that every child must follow this row.
            await self.session.flush()

            for task in result.decomposition.subtasks if result.decomposition else []:
                self.session.add(
                    GatewayTaskRecord(
                        request_id=request_id,
                        task_id=task.task_id,
                        description=task.description,
                        task_type=task.task_type.value,
                        difficulty=task.difficulty.value,
                        dependencies_json=task.dependencies,
                        input_from_json=task.input_from,
                        required_capabilities_json=task.required_capabilities,
                        constraints_json=task.constraints,
                        expected_output=task.expected_output,
                        goal_ids_json=task.goal_ids,
                    )
                )

            for item in execution_payloads:
                task_id = item.get("task_id", "unknown")
                self.session.add(
                    RouteDecisionRecord(
                        request_id=request_id,
                        task_id=task_id,
                        strategy=(
                            "fixed-fallback"
                            if item.get("fallback")
                            else result.request.metadata.get("router") or "runtime"
                        ),
                        selected_tier=item.get("route"),
                        selected_model=item.get("selected_model"),
                        confidence=item.get("confidence"),
                        fallback_models_json=[],
                        reason=item.get("failure_reason"),
                    )
                )
                executed_model = item.get("executed_model")
                self.session.add(
                    ModelExecutionRecord(
                        request_id=request_id,
                        task_id=task_id,
                        provider=item.get("provider") or self._provider_name(executed_model),
                        model=executed_model,
                        selected_model=item.get("selected_model"),
                        status=item.get("status", "failed"),
                        fallback=bool(item.get("fallback", False)),
                        failure_type=item.get("failure_type"),
                        failure_reason=item.get("failure_reason"),
                        input_tokens=int(item.get("input_tokens", 0)),
                        output_tokens=int(item.get("output_tokens", 0)),
                        cost_usd=float(item.get("cost_usd", 0.0)),
                        latency_ms=float(item.get("latency_ms", 0.0)),
                        result_payload=item.get("result"),
                    )
                )
                self.session.add(
                    UsageEventRecord(
                        request_id=request_id,
                        application_id=result.request.application_id,
                        provider=item.get("provider") or self._provider_name(executed_model),
                        model=executed_model,
                        input_tokens=int(item.get("input_tokens", 0)),
                        output_tokens=int(item.get("output_tokens", 0)),
                        cost_usd=float(item.get("cost_usd", 0.0)),
                        estimated=bool(item.get("cost_estimated", False)),
                        source=str(result.request.metadata.get("source", "unknown")),
                    )
                )

            if aggregation is not None:
                self.session.add(
                    AggregationResultRecord(
                        request_id=request_id,
                        strategy=aggregation.strategy,
                        status=aggregation.status.value,
                        final_answer=aggregation.final_answer,
                        contributions_json=[self._dump(item) for item in aggregation.contributions],
                        missing_goal_ids_json=aggregation.missing_goal_ids,
                        reason=aggregation.reason,
                    )
                )
            for trace in traces:
                self.session.add(
                    StageTraceRecord(
                        request_id=request_id,
                        stage=trace.stage,
                        strategy=trace.strategy,
                        model=trace.model,
                        status=trace.status,
                        input_tokens=trace.input_tokens,
                        output_tokens=trace.output_tokens,
                        cost_usd=trace.cost_usd,
                        latency_ms=trace.latency_ms,
                        error=trace.error,
                        metadata_json=trace.metadata,
                        started_at=trace.started_at,
                        finished_at=trace.finished_at,
                    )
                )
            await BillingService(self.session).debit_runtime_request(
                result.request.application_id, request_id, result.costs.total_cost, status,
                bill_measured_usage=str(result.request.metadata.get('client_metadata', {}).get('api_surface', '')).startswith('openai'),
            )
        except Exception:
            await self.session.rollback()
            raise
        return request_record

    async def get_request(self, request_id: str):
        return await self.repository.request(request_id)

    async def list_tasks(self, request_id: str):
        return await self.repository.tasks(request_id)

    async def list_route_decisions(self, request_id: str):
        return await self.repository.route_decisions(request_id)

    async def list_executions(self, request_id: str):
        return await self.repository.executions(request_id)

    async def get_aggregation(self, request_id: str):
        return await self.repository.aggregation(request_id)

    async def list_traces(self, request_id: str):
        return await self.repository.traces(request_id)

    @staticmethod
    def _request_status(result: PipelineResult) -> str:
        if not result.validation.accepted:
            return "failed"
        if result.aggregation is None:
            return "failed"
        return {
            "complete": "completed",
            "partial": "partial",
            "failed": "failed",
        }[result.aggregation.status.value]

    @staticmethod
    def _provider_name(model: str | None) -> str | None:
        if not model:
            return None
        if ":" in model:
            return model.split(":", 1)[0]
        return "offline"

    @staticmethod
    def _dump(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if is_dataclass(value):
            return asdict(value)
        return value


__all__ = ["GatewayPersistenceService", "IncidentRecord"]
