from __future__ import annotations

import json

from pydantic import BaseModel, Field

from src.shared.dtos.gateway import AggregationResult, AggregationStatus, EffectiveConstraints, GoalExtractionResult


class AggregationReport(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)


class AggregationValidator:
    """Structural checks on an AggregationResult, symmetric to
    DecompositionValidator's goal-contract checks (no LLM call)."""

    def validate(
        self,
        result: AggregationResult,
        goal_extraction: GoalExtractionResult | None = None,
        constraints: EffectiveConstraints | None = None,
    ) -> AggregationReport:
        errors: list[str] = []

        seen_task_ids: set[str] = set()
        for contribution in result.contributions:
            if contribution.task_id in seen_task_ids:
                errors.append(f"DUPLICATE_CONTRIBUTION:{contribution.task_id}")
            seen_task_ids.add(contribution.task_id)

        if result.status == AggregationStatus.COMPLETE:
            if not result.final_answer:
                errors.append("EMPTY_FINAL_ANSWER")
            elif constraints and constraints.constraints.output_format == "json":
                try:
                    json.loads(result.final_answer)
                except (TypeError, json.JSONDecodeError):
                    errors.append("INVALID_JSON_FINAL_ANSWER")
            if goal_extraction:
                covered = {
                    goal_id
                    for contribution in result.contributions
                    if contribution.used_in_final_answer
                    for goal_id in contribution.goal_ids
                }
                missing = {goal.goal_id for goal in goal_extraction.goals} - covered
                if missing:
                    errors.append(f"UNCOVERED_GOALS:{sorted(missing)}")

        if result.status == AggregationStatus.FAILED and result.final_answer:
            errors.append("FAILED_STATUS_WITH_FINAL_ANSWER")

        return AggregationReport(valid=not errors, errors=errors)
