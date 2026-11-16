from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.shared.dtos.gateway import OutputEvidence, PipelineResult
from src.shared.dtos.runtime_api import (
    RuntimeCost,
    RuntimeOperationError,
    RuntimeRequestResponse,
    RuntimeUsage,
    RuntimeUsageDetails,
    RuntimeUsageScope,
    RuntimeWarning,
)
from src.shared.models.runtime import RuntimeOperationRecord

_PIPELINE_STAGES = {"constraint_resolution", "understand", "decompose", "aggregate", "attachment_analysis"}


class RuntimeResponseService:
    @classmethod
    def from_result(
        cls,
        result: PipelineResult,
        snapshot: dict[str, Any],
        *,
        wall_latency_ms: float,
        operation_started_at: datetime | None = None,
        finished_at: datetime | None = None,
        execution_mode: str | None = None,
    ) -> RuntimeRequestResponse:
        executions = result.execution_results or []
        successful = [item for item in executions if item.status == "success"]
        model_catalog = {
            name: model
            for model in snapshot.get("models", [])
            for name in (model.get("id"), model.get("name"))
            if name
        }
        models_used: list[str] = []
        providers_used: list[str] = []
        for execution in successful:
            if not execution.executed_model:
                continue
            identity = model_catalog.get(execution.executed_model, {})
            model_id = identity.get("id", execution.executed_model)
            provider_name = identity.get("provider_name") or execution.provider
            if model_id not in models_used:
                models_used.append(model_id)
            if provider_name and provider_name not in providers_used:
                providers_used.append(provider_name)

        exec_input = sum(item.input_tokens for item in executions)
        exec_output = sum(item.output_tokens for item in executions)
        pipeline_input = exec_input
        pipeline_output = exec_output
        usage_complete = all(item.usage_complete for item in executions)
        for trace in result.traces:
            if trace.stage not in _PIPELINE_STAGES or trace.status == "skipped":
                continue
            is_model_call = bool(trace.model) or bool(trace.metadata.get("semantic_model_call"))
            if not is_model_call:
                continue
            pipeline_input += trace.input_tokens
            pipeline_output += trace.output_tokens
            usage_complete = usage_complete and bool(trace.metadata.get("usage_complete", False))

        usage_details = cls._usage_details(result, executions)

        status = "failed"
        if result.validation.accepted and result.aggregation is not None:
            status = {
                "complete": "completed",
                "partial": "partial",
                "failed": "failed",
            }[result.aggregation.status.value]
        output = result.aggregation.final_answer if result.aggregation else None
        evidence = result.output_evidence or (result.aggregation.output_evidence if result.aggregation else None)
        public_terminal = (
            str(result.request.metadata.get("client_metadata", {}).get("api_surface", "")).startswith("openai")
            and evidence is not None and (evidence.finish_reason in {"length", "content_filter", "tool_calls"}
                or bool(result.openai_choices and result.openai_choices[0].get("message", {}).get("refusal")))
            and isinstance(output, str)
        )
        if status in {"completed", "partial"} and not (isinstance(output, str) and output.strip()) and not public_terminal:
            status = "failed"
            output = None

        warnings = cls._warnings(snapshot)
        if str(result.request.metadata.get("client_metadata", {}).get("api_surface", "")).startswith("openai"):
            for item in result.attachments or []:
                if item.status != "analyzed" or item.error:
                    warnings.append(RuntimeWarning(code="ATTACHMENT_OMITTED",
                        message="An attachment could not be fully analyzed under the selected partial-response policy.",
                        details={"attachment_id": item.attachment_id}))
        if status == "partial":
            warnings.append(
                RuntimeWarning(
                    code="PARTIAL_RESPONSE",
                    message="Some requested goals could not be completed.",
                    details={"missing_goal_ids": result.aggregation.missing_goal_ids if result.aggregation else []},
                )
            )

        error = None
        if status == "failed":
            failed_tasks = [item.task_id for item in executions if item.status == "failed"]
            missing_goals = result.aggregation.missing_goal_ids if result.aggregation else []
            code = (
                "REQUEST_REJECTED"
                if not result.validation.accepted
                else "PROVIDER_EXECUTION_FAILED"
                if failed_tasks
                else "AGGREGATION_FAILED"
            )
            details: dict[str, Any] = {}
            if failed_tasks:
                details["failed_task_ids"] = failed_tasks
            if missing_goals:
                details["missing_goal_ids"] = missing_goals
            error = RuntimeOperationError(
                code=code,
                message="The request could not be completed.",
                retryable=False,
                details=(
                    {
                        "issues": [
                            {"code": issue.code, "field": issue.field}
                            for issue in result.validation.errors
                        ]
                    }
                    if code == "REQUEST_REJECTED"
                    else details
                ),
            )

        now = datetime.now(UTC)
        started = operation_started_at or min((trace.started_at for trace in result.traces), default=now)
        finished = finished_at or max((trace.finished_at for trace in result.traces), default=now)
        from src.backend_ai.services.runtime_stream_service import current_stream
        stream = current_stream()
        return RuntimeRequestResponse(
            request_id=result.request.request_id,
            application_id=result.request.application_id,
            status=status,
            execution_mode=execution_mode or snapshot.get("execution_mode", "offline"),
            output=output,
            openai_choices=result.openai_choices,
            openai_output_order=list(stream.output_order) if stream is not None else None,
            models_used=models_used,
            providers_used=providers_used,
            usage=RuntimeUsage(
                final_output=cls._final_usage(result, successful),
                execution=RuntimeUsageScope(
                    input_tokens=exec_input,
                    output_tokens=exec_output,
                    total_tokens=exec_input + exec_output,
                ),
                pipeline=RuntimeUsageScope(
                    input_tokens=pipeline_input,
                    output_tokens=pipeline_output,
                    total_tokens=pipeline_input + pipeline_output,
                ),
                complete=usage_complete,
            ),
            usage_details=usage_details,
            output_evidence=result.output_evidence
            or (result.aggregation.output_evidence if result.aggregation is not None else None),
            cost=RuntimeCost(
                estimated_cost=result.costs.total_cost,
                estimated=bool(result.costs.estimated or not usage_complete),
            ),
            latency_ms=round(max(0.0, wall_latency_ms), 3),
            fallback_used=any(item.fallback for item in executions),
            config_version=int((snapshot.get("config") or {}).get("version", 1)),
            started_at=started,
            finished_at=finished,
            warnings=warnings,
            error=error,
        )

    @staticmethod
    def sanitize_for_persistence(result: PipelineResult) -> PipelineResult:
        """Drop untrusted provider exception text from public-operation traces."""
        sanitized = result.model_copy(deep=True)
        for execution in sanitized.execution_results or []:
            if execution.failure_reason:
                execution.failure_reason = "Provider attempt failed."
        for trace in sanitized.traces:
            if trace.error:
                trace.error = "Stage execution failed."
        return sanitized

    @classmethod
    def from_operation(cls, operation: RuntimeOperationRecord) -> RuntimeRequestResponse:
        if operation.public_result_json is not None:
            return RuntimeRequestResponse.model_validate(operation.public_result_json)
        status = "indeterminate" if operation.status == "indeterminate" else "running"
        warnings = cls._warnings(operation.config_snapshot_json or {})
        error = None
        if status == "indeterminate":
            error = RuntimeOperationError(
                code="REQUEST_OUTCOME_UNKNOWN",
                message="The request outcome could not be confirmed.",
                retryable=False,
                details={},
            )
        return RuntimeRequestResponse(
            request_id=operation.request_id,
            application_id=operation.application_id,
            status=status,
            execution_mode=operation.execution_mode,
            output=None,
            models_used=[],
            providers_used=[],
            usage=None,
            cost=None,
            latency_ms=None,
            fallback_used=False,
            config_version=operation.config_version,
            started_at=operation.started_at,
            finished_at=None,
            warnings=warnings,
            error=error,
        )

    @staticmethod
    def _warnings(snapshot: dict[str, Any]) -> list[RuntimeWarning]:
        warnings: list[RuntimeWarning] = []
        strategies = snapshot.get("strategies") or {}
        requested_router = strategies.get("router_requested")
        if requested_router and requested_router != "database":
            warnings.append(
                RuntimeWarning(
                    code="ROUTER_FORCED_DATABASE",
                    message="Public requests always use the application model pool.",
                    details={"requested": requested_router, "effective": "database"},
                )
            )
        if strategies.get("substituted"):
            warnings.append(
                RuntimeWarning(
                    code="PIPELINE_STRATEGY_SUBSTITUTED",
                    message=f"The {snapshot.get('pipeline', 'offline')} pipeline ran its own stage in place of a "
                    "configured strategy it does not run.",
                    details=strategies["substituted"],
                )
            )
        config = snapshot.get("config") or {}
        budget = snapshot.get("budget") or {}
        if budget.get("state") == "warning":
            warnings.append(
                RuntimeWarning(
                    code="BUDGET_THRESHOLD_REACHED",
                    message="Application monthly budget has reached its warning threshold.",
                    details=budget,
                )
            )
        elif budget.get("state") == "exhausted":
            warnings.append(
                RuntimeWarning(
                    code="BUDGET_LIMIT_REACHED",
                    message="Application monthly budget has been exhausted.",
                    details=budget,
                )
            )
        elif config.get("budget_ceiling") is not None and not budget:
            # Keep a truthful compatibility warning for legacy receipts created
            # before the monthly budget snapshot was added.
            warnings.append(
                RuntimeWarning(
                    code="BUDGET_SNAPSHOT_UNAVAILABLE",
                    message="The application budget snapshot was unavailable for this request.",
                    details={"budget_ceiling": config["budget_ceiling"]},
                )
            )
        return warnings

    @staticmethod
    def _final_usage(result, successful) -> RuntimeUsageScope | None:
        # An LLM aggregation call is the final producer only when used, never
        # when it failed and deterministic concatenation supplied the answer.
        aggregate = next((trace for trace in reversed(result.traces)
            if trace.stage == "aggregate" and trace.model and trace.status == "success"), None)
        if aggregate is not None:
            if not aggregate.metadata.get('usage_complete'):
                return None
            inputs, outputs = aggregate.input_tokens, aggregate.output_tokens
        else:
            used = {part.task_id for part in (result.aggregation.contributions if result.aggregation else [])
                if part.used_in_final_answer}
            producers = [item for item in successful if item.task_id in used] if used else successful
            if not producers or not all(item.final_usage_complete if item.final_usage_complete is not None
                                       else item.usage_complete for item in producers):
                return None
            inputs = sum(item.final_input_tokens if item.final_input_tokens is not None else item.input_tokens for item in producers)
            outputs = sum(item.final_output_tokens if item.final_output_tokens is not None else item.output_tokens for item in producers)
        return RuntimeUsageScope(input_tokens=inputs, output_tokens=outputs, total_tokens=inputs + outputs)

    @staticmethod
    def _usage_details(result: PipelineResult, executions) -> RuntimeUsageDetails | None:
        evidence: list[OutputEvidence | None] = []
        evidence.extend(
            item.output_evidence
            for item in executions
        )
        for trace in result.traces:
            if trace.stage not in _PIPELINE_STAGES or trace.status == "skipped":
                continue
            is_model_call = bool(trace.model) or bool(trace.metadata.get("semantic_model_call"))
            if not is_model_call:
                continue
            raw = trace.metadata.get("output_evidence")
            try:
                evidence.append(OutputEvidence.model_validate(raw) if raw is not None else None)
            except (TypeError, ValueError):
                evidence.append(None)
        if not evidence:
            return None
        totals = {}
        for field in ("cached_tokens", "cache_write_tokens", "reasoning_tokens"):
            values = [getattr(item, field) if item is not None else None for item in evidence]
            totals[field] = sum(values) if all(value is not None for value in values) else None
        return RuntimeUsageDetails(**totals, complete=all(value is not None for value in totals.values()))


def public_generation_usage(receipt: RuntimeRequestResponse) -> RuntimeUsageScope | None:
    usage = receipt.usage
    if usage is None:
        return None
    return usage.final_output or (usage.execution if usage.complete else None)
