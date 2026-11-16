from __future__ import annotations

import json

from src.backend_ai.gateway.aggregation.base import AggregationOutcome, BaseAggregator, SubtaskExecutionResult
from src.shared.dtos.gateway import (
    AggregationResult,
    AggregationStatus,
    DecompositionResult,
    EffectiveConstraints,
    GatewayRequest,
    GoalExtractionResult,
    OutputEvidence,
    SubtaskContribution,
)


class NoAggregator(BaseAggregator):
    """Default for the single-subtask case (decomposition.decomposition_applied
    is False): the one subtask's result *is* the final answer, no LLM call.
    Mirrors NoDecomposer's role — always available, zero cost."""

    name = "none"

    async def aggregate(
        self,
        request: GatewayRequest,
        decomposition: DecompositionResult,
        execution_results: list[SubtaskExecutionResult],
        constraints: EffectiveConstraints,
        goal_extraction: GoalExtractionResult | None = None,
    ) -> AggregationOutcome:
        if len(execution_results) != 1:
            return AggregationOutcome(
                result=AggregationResult(
                    original_request_id=request.request_id,
                    status=AggregationStatus.FAILED,
                    final_answer=None,
                    contributions=[
                        SubtaskContribution(
                            task_id=r.task_id,
                            status=r.status,  # type: ignore[arg-type]
                            used_in_final_answer=False,
                        )
                        for r in execution_results
                    ],
                    strategy=self.name,
                    reason=(
                        f"NoAggregator expects exactly 1 subtask, got {len(execution_results)}; "
                        "use ConcatAggregator or LLMAggregator for multi-subtask decompositions."
                    ),
                ),
                strategy=self.name,
                error="subtask_count_mismatch",
            )

        only = execution_results[0]
        output_evidence = getattr(only, "output_evidence", None) or OutputEvidence()
        task = decomposition.subtasks[0] if decomposition.subtasks else None
        goal_ids = task.goal_ids if task else []
        if only.status == "failed":
            return AggregationOutcome(
                result=AggregationResult(
                    original_request_id=request.request_id,
                    status=AggregationStatus.FAILED,
                    final_answer=None,
                    contributions=[
                        SubtaskContribution(
                            task_id=only.task_id, goal_ids=goal_ids, status="failed", used_in_final_answer=False
                        )
                    ],
                    missing_goal_ids=goal_ids,
                    strategy=self.name,
                    reason="The only subtask failed; no result to return.",
                ),
                strategy=self.name,
            )

        if constraints.constraints.output_format == "json":
            try:
                json.loads(only.result or "")
            except (TypeError, json.JSONDecodeError):
                return AggregationOutcome(
                    result=AggregationResult(
                        original_request_id=request.request_id,
                        status=AggregationStatus.FAILED,
                        final_answer=None,
                        contributions=[
                            SubtaskContribution(
                                task_id=only.task_id,
                                goal_ids=goal_ids,
                                status="success",
                                used_in_final_answer=False,
                                excerpt=(only.result or "")[:200],
                            )
                        ],
                        strategy=self.name,
                        reason="Strict JSON output requested, but the pass-through result is not valid JSON.",
                    ),
                    strategy=self.name,
                    error="invalid_json_final_answer",
                )
        return AggregationOutcome(
            result=AggregationResult(
                original_request_id=request.request_id,
                status=AggregationStatus.COMPLETE,
                final_answer=only.result,
                contributions=[
                    SubtaskContribution(
                        task_id=only.task_id,
                        goal_ids=goal_ids,
                        status="success",
                        used_in_final_answer=True,
                        excerpt=(only.result or "")[:200],
                    )
                ],
                strategy=self.name,
                reason="Single subtask; its result passes through unchanged.",
                output_evidence=output_evidence,
            ),
            strategy=self.name,
            output_evidence=output_evidence,
        )
