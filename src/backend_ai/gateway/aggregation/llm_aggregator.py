from __future__ import annotations

import json

from src.backend_ai.dtos.llm import LLMAggregationResult
from src.backend_ai.gateway.aggregation.base import AggregationOutcome, BaseAggregator, SubtaskExecutionResult
from src.backend_ai.gateway.aggregation.concat_aggregator import ConcatAggregator
from src.backend_ai.gateway.aggregation.validator import AggregationValidator
from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.llm.client import StructuredModelClient
from src.shared.dtos.gateway import (
    AggregationResult,
    AggregationStatus,
    DecompositionResult,
    EffectiveConstraints,
    GatewayRequest,
    GoalExtractionResult,
    ModelUsage,
    StructuredModelResponse,
    SubtaskContribution,
)


class LLMAggregator(BaseAggregator):
    """Synthesizes the subtask results into one final answer with a single
    LLM call. Falls back to ConcatAggregator on any error or failed
    AggregationValidator check — same one-retry-then-deterministic-fallback
    pattern LLMDecomposer uses for decompose."""

    name = "llm"
    supports_json_output = True

    def __init__(
        self,
        client: StructuredModelClient,
        config: GatewayConfig | None = None,
        fallback: BaseAggregator | None = None,
    ) -> None:
        self.client = client
        self.config = config or GatewayConfig()
        self.fallback = fallback or ConcatAggregator()
        self.validator = AggregationValidator()

    async def aggregate(
        self,
        request: GatewayRequest,
        decomposition: DecompositionResult,
        execution_results: list[SubtaskExecutionResult],
        constraints: EffectiveConstraints,
        goal_extraction: GoalExtractionResult | None = None,
    ) -> AggregationOutcome:
        succeeded = [r for r in execution_results if r.status == "success" and r.result]
        failed = [r for r in execution_results if r.status != "success"]

        if not succeeded:
            fallback_outcome = await self.fallback.aggregate(
                request, decomposition, execution_results, constraints, goal_extraction
            )
            fallback_outcome.strategy = f"{self.name}->{self.fallback.name}"
            fallback_outcome.fallback = True
            fallback_outcome.error = "No subtask succeeded; nothing to synthesize."
            return fallback_outcome

        task_by_id = {task.task_id: task for task in decomposition.subtasks}
        results_payload = [
            {
                "task_id": r.task_id,
                "goal_ids": task_by_id[r.task_id].goal_ids if r.task_id in task_by_id else [],
                "result": r.result,
            }
            for r in succeeded
        ]
        failed_payload = [
            {"task_id": r.task_id, "goal_ids": task_by_id[r.task_id].goal_ids if r.task_id in task_by_id else []}
            for r in failed
        ]
        constraint_values = constraints.constraints.model_dump(mode="json", exclude_none=True)
        output_format_instruction = (
            "Return only valid JSON as a raw JSON value. Do not wrap it in Markdown fences or add explanatory text."
            if constraints.constraints.output_format == "json"
            else "Return only the final answer in the requested output format."
        )
        prompt = (
            "Synthesize ONE final answer to the ORIGINAL REQUEST from the subtask results below. "
            "Use only information present in the subtask results — do not invent content for a "
            "failed subtask; instead note in the answer that part could not be completed if it "
            "materially affects the response. Respect the output constraints exactly "
            "(format, language, max output tokens).\n\n"
            f"OUTPUT FORMAT INSTRUCTION: {output_format_instruction}\n"
            f"ORIGINAL REQUEST: {request.focus_text()}\n"
            f"SUCCESSFUL SUBTASK RESULTS: {json.dumps(results_payload, ensure_ascii=False)}\n"
            f"FAILED SUBTASKS (excluded, do not fabricate their content): "
            f"{json.dumps(failed_payload, ensure_ascii=False)}\n"
            f"OUTPUT CONSTRAINTS: {json.dumps(constraint_values, ensure_ascii=False)}"
        )

        usage = ModelUsage()
        usage_complete = False
        public = str(request.metadata.get("client_metadata", {}).get("api_surface", "")).startswith("openai")
        try:
            if public:
                from src.backend_ai.gateway.providers.base import GenerationOptions
                caller_cap = request.metadata.get("openai_hard_completion_token_limit")
                application_cap = constraints.constraints.max_output_tokens
                caps = [cap for cap in (caller_cap, application_cap) if cap is not None]
                cap = min(caps) if caps else None
                raw = await self.client.generate_output(prompt, self.config.aggregator_model,
                    self.config.llm_timeout_seconds, options=GenerationOptions(max_output_tokens=cap,
                        hard_completion_token_limit=cap))
                response = StructuredModelResponse(data={"final_answer": raw.text}, model=self.config.aggregator_model,
                    usage=ModelUsage(input_tokens=raw.input_tokens, output_tokens=raw.output_tokens),
                    usage_complete=raw.usage_received, output_evidence=raw.output_evidence)
            else:
                response = await self.client.generate(
                    prompt, LLMAggregationResult, self.config.aggregator_model, self.config.llm_timeout_seconds
                )
            usage = response.usage
            usage_complete = response.usage_complete
            final_answer = response.data.get("final_answer", "")
            missing_goal_ids = [
                goal_id
                for r in failed
                for goal_id in (task_by_id[r.task_id].goal_ids if r.task_id in task_by_id else [])
            ]
            status = AggregationStatus.PARTIAL if missing_goal_ids else AggregationStatus.COMPLETE
            if missing_goal_ids and not constraints.constraints.allow_partial_response:
                raise ValueError(
                    f"{len(missing_goal_ids)} goal(s) missing a successful subtask and allow_partial_response=false"
                )
            result = AggregationResult(
                original_request_id=request.request_id,
                status=status,
                final_answer=final_answer,
                contributions=[
                    SubtaskContribution(
                        task_id=r.task_id,
                        goal_ids=task_by_id[r.task_id].goal_ids if r.task_id in task_by_id else [],
                        status="success",
                        used_in_final_answer=True,
                        excerpt=(r.result or "")[:200],
                    )
                    for r in succeeded
                ]
                + [
                    SubtaskContribution(
                        task_id=r.task_id,
                        goal_ids=task_by_id[r.task_id].goal_ids if r.task_id in task_by_id else [],
                        status="failed",
                        used_in_final_answer=False,
                    )
                    for r in failed
                ],
                missing_goal_ids=missing_goal_ids,
                strategy=self.name,
                reason=f"LLM synthesis over {len(succeeded)} successful subtask result(s).",
                output_evidence=response.output_evidence,
            )
            report = self.validator.validate(result, goal_extraction, constraints)
            if not report.valid and not (public and response.output_evidence.finish_reason in {"length", "content_filter"}):
                raise ValueError(f"Aggregation validation failed: {report.errors}")
            return AggregationOutcome(
                result=result,
                strategy=self.name,
                model=response.model,
                usage=ModelUsage(input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens),
                usage_complete=usage_complete,
                output_evidence=response.output_evidence,
            )
        except Exception as exc:
            if public:
                # An unbounded concatenation is not an equivalent substitute
                # for a capped final producer. Never emit a second answer.
                raise
            fallback_outcome = await self.fallback.aggregate(
                request, decomposition, execution_results, constraints, goal_extraction
            )
            fallback_outcome.strategy = f"{self.name}->{self.fallback.name}"
            fallback_outcome.model = self.config.aggregator_model
            fallback_outcome.fallback = True
            fallback_outcome.error = str(exc)
            fallback_outcome.usage = usage
            fallback_outcome.usage_complete = usage_complete
            return fallback_outcome
