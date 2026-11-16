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
    SubTask,
    SubtaskContribution,
)


class ConcatAggregator(BaseAggregator):
    """Offline baseline (no LLM call): deterministically joins subtask
    results into one final answer. Plays the same role NoDecomposer/
    DeterministicGoalDecomposer play for decompose — always available, and
    the fallback target when LLMAggregator errors or fails validation."""

    name = "deterministic"
    supports_json_output = True

    async def aggregate(
        self,
        request: GatewayRequest,
        decomposition: DecompositionResult,
        execution_results: list[SubtaskExecutionResult],
        constraints: EffectiveConstraints,
        goal_extraction: GoalExtractionResult | None = None,
    ) -> AggregationOutcome:
        results_by_id = {r.task_id: r for r in execution_results}
        depended_on: set[str] = {
            dep for task in decomposition.subtasks for dep in task.dependencies
        }
        if decomposition.execution_mode == "parallel":
            include_ids = [task.task_id for task in decomposition.subtasks]
        else:
            # sequential/hybrid/single: leaf tasks only — an intermediate task's
            # output was already consumed as input by its dependents via
            # input_from, so printing it again would usually be redundant.
            include_ids = [task.task_id for task in decomposition.subtasks if task.task_id not in depended_on]
            if not include_ids:
                include_ids = [task.task_id for task in decomposition.subtasks]

        contributions: list[SubtaskContribution] = []
        missing_goal_ids: list[str] = []
        sections: list[str] = []
        json_results: list[dict[str, str]] = []

        for task_index, task in enumerate(decomposition.subtasks, 1):
            result = results_by_id.get(task.task_id)
            status = result.status if result else "failed"
            used = task.task_id in include_ids and status == "success"
            contributions.append(
                SubtaskContribution(
                    task_id=task.task_id,
                    goal_ids=task.goal_ids,
                    status=status,  # type: ignore[arg-type]
                    used_in_final_answer=used,
                    excerpt=(result.result or "")[:200] if result and result.result else None,
                )
            )
            if status == "failed":
                missing_goal_ids.extend(task.goal_ids)
            elif used and result is not None and result.result:
                sections.append(
                    f"## {task_index}. {_section_title(task, goal_extraction)}\n\n{result.result}"
                    if len(include_ids) > 1
                    else result.result
                )
                json_results.append({"task_id": task.task_id, "result": result.result})

        allow_partial = constraints.constraints.allow_partial_response
        if missing_goal_ids and not allow_partial:
            return AggregationOutcome(
                result=AggregationResult(
                    original_request_id=request.request_id,
                    status=AggregationStatus.FAILED,
                    final_answer=None,
                    contributions=contributions,
                    missing_goal_ids=missing_goal_ids,
                    strategy=self.name,
                    reason=(
                        "Cannot return partial result: allow_partial_response=false and "
                        f"{len(missing_goal_ids)} goal(s) have no successful subtask."
                    ),
                ),
                strategy=self.name,
            )

        terminal = next((r for r in execution_results if r.status == "success"
            and getattr(r, "output_evidence", OutputEvidence()).finish_reason in {"length", "content_filter"}), None)
        public_request = str(request.metadata.get("client_metadata", {}).get("api_surface", "")).startswith("openai")
        if not sections and terminal is not None and public_request:
            evidence = terminal.output_evidence
            return AggregationOutcome(result=AggregationResult(
                original_request_id=request.request_id, status=AggregationStatus.COMPLETE, final_answer="",
                contributions=contributions, strategy=self.name, reason="Provider reported a terminal truncated/filtered output.",
                output_evidence=evidence,
            ), strategy=self.name, output_evidence=evidence)

        if not sections:
            return AggregationOutcome(
                result=AggregationResult(
                    original_request_id=request.request_id,
                    status=AggregationStatus.FAILED,
                    final_answer=None,
                    contributions=contributions,
                    missing_goal_ids=missing_goal_ids,
                    strategy=self.name,
                    reason="No subtask produced a usable result to aggregate.",
                ),
                strategy=self.name,
            )

        status = AggregationStatus.PARTIAL if missing_goal_ids else AggregationStatus.COMPLETE
        included_evidence = [
            getattr(results_by_id[task_id], "output_evidence", OutputEvidence())
            for task_id in include_ids
            if task_id in results_by_id and results_by_id[task_id].status == "success"
        ]
        finish_reason = _conservative_finish_reason(included_evidence)
        counters = {}
        for name in ("cached_tokens", "cache_write_tokens", "reasoning_tokens"):
            values = [getattr(item, name) for item in included_evidence]
            counters[name] = sum(values) if values and all(value is not None for value in values) else None
        output_evidence = OutputEvidence(finish_reason=finish_reason, **counters)
        reason = (
            f"Deterministic concatenation of {len(sections)} subtask result(s)"
            f" ({decomposition.execution_mode} mode)"
            + (f"; {len(missing_goal_ids)} goal(s) missing, returned partially." if missing_goal_ids else ".")
        )
        if constraints.constraints.output_format == "json":
            json_payload: dict[str, object] = {"results": json_results}
            if missing_goal_ids:
                json_payload["missing_goal_ids"] = missing_goal_ids
            final_answer = json.dumps(json_payload, ensure_ascii=False)
        else:
            final_answer = "\n\n".join(sections)
        return AggregationOutcome(
            result=AggregationResult(
                original_request_id=request.request_id,
                status=status,
                final_answer=final_answer,
                contributions=contributions,
                missing_goal_ids=missing_goal_ids,
                strategy=self.name,
                reason=reason,
                output_evidence=output_evidence,
            ),
            strategy=self.name,
            output_evidence=output_evidence,
        )


_MAX_TITLE_CHARS = 80


def _section_title(task: SubTask, goal_extraction: GoalExtractionResult | None) -> str:
    """Label each section with the user's own wording of the goal ("Tóm tắt
    đoạn văn...") instead of an opaque "Task N"."""
    goals = {goal.goal_id: goal for goal in goal_extraction.goals} if goal_extraction else {}
    texts = [goals[goal_id].normalized_text for goal_id in task.goal_ids if goal_id in goals]
    lines = (texts[0] if texts else task.description).strip().splitlines()
    text = lines[0].rstrip(" .:;") if lines else ""
    if len(text) > _MAX_TITLE_CHARS:
        text = text[: _MAX_TITLE_CHARS - 1].rstrip() + "…"
    return text or task.task_id


def _conservative_finish_reason(evidence: list[OutputEvidence]) -> str:
    reasons = {item.finish_reason for item in evidence}
    if "content_filter" in reasons:
        return "content_filter"
    if "length" in reasons:
        return "length"
    if "unknown" in reasons or not evidence:
        return "unknown"
    return "stop"
