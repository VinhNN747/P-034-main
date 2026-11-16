"""Chains decompose (RequestPipeline) -> route -> execute -> aggregate into
one call, and folds route/aggregate into the same trace/cost accounting the
pipeline already produces for understand/decompose.

Kept separate from RequestPipeline rather than merged into it (see
reports/AGGREGATE_TRACE_EVALUATE_DESIGN.md section 6):
- RequestPipeline's result cache is keyed on deterministic inputs
  (prompt/config/strategy names); route+execute cost/latency depend on a
  real model call and are not safe to cache the same way.
- RequestPipeline stays usable standalone for callers that only need
  understand+decompose (e.g. the existing EvaluationRunner default), without
  paying for route+execute.
- Route lives in `src.backend_ai.agents.routing`, a different top-level package from
  `src.backend_ai.gateway`; this orchestrator is the single place that imports both,
  instead of `gateway.pipeline` and `agents.routing` importing each other
  directly.
"""

from __future__ import annotations

from dataclasses import asdict, replace

from src.backend_ai.agents.routing.base import BaseRouter
from src.backend_ai.agents.routing.executor import ExecutionContext, SubtaskExecutor
from src.backend_ai.agents.routing.router import ExecutionConfig, execution_trace_rows
from src.backend_ai.gateway.aggregation.base import BaseAggregator
from src.backend_ai.gateway.aggregation.selector import resolve_aggregator
from src.backend_ai.gateway.core.circuit_breaker import CircuitBreaker
from src.backend_ai.gateway.multimodal.digest import execution_context
from src.backend_ai.gateway.observability.cost_tracker import summarize_costs
from src.backend_ai.gateway.observability.trace import TraceCollector, execution_trace_rows_to_stage_traces, trace_timer
from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.shared.dtos.gateway import ExecutionResultTrace, GatewayRequest, PipelineResult
from src.shared.services.gateway_persistence_service import GatewayPersistenceService


class GatewayOrchestrator:
    def __init__(
        self,
        pipeline: RequestPipeline,
        routers: dict[str, BaseRouter],
        aggregators: dict[str, BaseAggregator],
        persistence: GatewayPersistenceService | None = None,
        providers: dict | None = None,
        execution_config: ExecutionConfig | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.routers = routers
        self.aggregators = aggregators
        self.persistence = persistence
        self.providers = providers
        self.execution_config = execution_config
        self.circuit_breaker = circuit_breaker

    async def run(
        self,
        request: GatewayRequest,
        *,
        understander: str = "rule",
        decomposer: str = "none",
        router: str = "deterministic",
        aggregator: str = "auto",
    ) -> PipelineResult:
        if router not in self.routers:
            raise ValueError(f"Unknown router '{router}'. Available: {sorted(self.routers)}")
        if aggregator != "auto" and aggregator not in self.aggregators:
            raise ValueError(f"Unknown aggregator '{aggregator}'. Available: {sorted(self.aggregators)}")
        request.metadata["aggregator_requested"] = aggregator

        result = await self.pipeline.run(request, understander=understander, decomposer=decomposer)
        from src.backend_ai.services.runtime_stream_service import current_stream
        if stream := current_stream():
            stream.partial_result = result.model_copy(deep=True)
        if result.decomposition is None:
            if self.persistence is not None:
                await self.persistence.save_run(result)
            return result

        strict_json = bool(
            result.effective_constraints and result.effective_constraints.constraints.output_format == "json"
        )
        selection = resolve_aggregator(
            aggregator,
            len(result.decomposition.subtasks),
            self.aggregators,
            strict_json=strict_json,
        )
        request.metadata.update(
            {
                "aggregator_requested": selection.requested,
                "aggregator_effective": selection.selected,
                "aggregator_selection_automatic": selection.automatic,
                "aggregator_selection_reason": selection.reason,
            }
        )
        if selection.selected not in self.aggregators:
            raise ValueError(f"Unknown aggregator '{selection.selected}'. Available: {sorted(self.aggregators)}")

        execution_config = self.execution_config or ExecutionConfig.from_settings()
        effective_constraints = result.effective_constraints
        public = str(request.metadata.get("client_metadata", {}).get("api_surface", "")).startswith("openai")
        if effective_constraints is not None and execution_config.generation_options_enabled:
            execution_config = replace(
                execution_config,
                max_output_tokens=effective_constraints.constraints.max_output_tokens,
                hard_completion_token_limit=(effective_constraints.constraints.max_output_tokens if public
                    else request.metadata.get("openai_hard_completion_token_limit")),
            )
        from src.backend_ai.services.runtime_stream_service import current_stream
        terminal_ids = tuple(task.task_id for task in result.decomposition.subtasks
            if result.decomposition.execution_mode == "parallel" or not any(
                task.task_id in other.dependencies for other in result.decomposition.subtasks))
        public = str(request.metadata.get("client_metadata", {}).get("api_surface", "")).startswith("openai")
        execution_config = replace(execution_config,
            stream_output=current_stream() is not None and selection.selected in {"none", "deterministic"}
                and not strict_json,
            stream_task_ids=terminal_ids,
            shared_completion_budget=public and selection.selected in {"none", "deterministic"}
                and execution_config.hard_completion_token_limit is not None)
        executor = SubtaskExecutor(
            self.routers[router],
            providers=self.providers,
            execution_config=execution_config,
            circuit_breaker=self.circuit_breaker,
            pricing=self.pipeline.pricing,
        )
        constraints = effective_constraints.constraints if effective_constraints is not None else request.constraints
        execution_results = await executor.aexecute_subtasks(
            result.decomposition.subtasks,
            constraints=constraints,
            context=ExecutionContext(
                original_prompt=request.focus_text(),
                request_context=request.context,
                conversation=request.conversation_text() or None,
                attachments=execution_context(result.attachments) or None,
                source_material=_shared_source_material(result),
                route_hints={"quality_priority": constraints.quality_priority},
            ),
        )

        rows = execution_trace_rows(result.decomposition.subtasks, execution_results)
        route_traces = execution_trace_rows_to_stage_traces(request.request_id, rows)
        result.execution_results = [
            ExecutionResultTrace(
                **{key: value for key, value in asdict(r).items() if key in ExecutionResultTrace.model_fields}
            )
            for r in execution_results
        ]

        aggregator_impl = self.aggregators[selection.selected]
        started, timer = trace_timer()
        outcome = await aggregator_impl.aggregate(
            request,
            result.decomposition,
            execution_results,
            result.effective_constraints,
            result.goal_extraction,
        )
        aggregate_cost, aggregate_estimated = self.pipeline.pricing.calculate_usage(
            outcome.model or "offline", outcome.usage.input_tokens, outcome.usage.output_tokens,
            outcome.output_evidence,
        )
        collector = TraceCollector(request.request_id)
        collector.add(
            stage="aggregate",
            strategy=outcome.strategy,
            model=outcome.model,
            started_at=started,
            timer_started=timer,
            input_tokens=outcome.usage.input_tokens,
            output_tokens=outcome.usage.output_tokens,
            cost_usd=aggregate_cost,
            status="fallback" if outcome.fallback else ("error" if outcome.error else "success"),
            error=outcome.error,
            metadata={
                "aggregation_status": outcome.result.status.value,
                "cost_estimated": aggregate_estimated,
                "aggregator_requested": selection.requested,
                "aggregator_effective": selection.selected,
                "aggregator_selection_automatic": selection.automatic,
                "aggregator_selection_reason": selection.reason,
                "usage_complete": outcome.usage_complete,
                "output_evidence": outcome.output_evidence.model_dump(mode="json"),
            },
        )

        result.aggregation = outcome.result
        result.output_evidence = outcome.output_evidence
        result.traces = [*result.traces, *route_traces, *collector.traces]
        result.costs = summarize_costs(result.traces)
        if self.persistence is not None:
            await self.persistence.save_run(result, execution_results)
        return result


def _shared_source_material(result: PipelineResult) -> str | None:
    """A single undecomposed task already carries the whole prompt as its
    description, so only split requests need the material passed alongside."""
    if result.goal_extraction is None or result.decomposition is None:
        return None
    if not result.decomposition.decomposition_applied:
        return None
    return result.goal_extraction.source_material
