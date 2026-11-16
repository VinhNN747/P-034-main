from __future__ import annotations

from src.backend_ai.gateway.decomposition.base import BaseDecomposer, DecompositionOutcome
from src.shared.dtos.gateway import (
    DecompositionResult,
    EffectiveConstraints,
    GatewayRequest,
    GoalExtractionResult,
    RequestProfile,
    SemanticAnalysis,
    SubTask,
    TaskType,
)


class NoDecomposer(BaseDecomposer):
    name = "none"

    async def decompose(
        self,
        request: GatewayRequest,
        analysis: SemanticAnalysis,
        profile: RequestProfile,
        constraints: EffectiveConstraints,
        reason: str,
        goal_extraction: GoalExtractionResult | None = None,
    ) -> DecompositionOutcome:
        constraint_values = constraints.constraints.model_dump(mode="json", exclude_none=True)
        task = SubTask(
            task_id="task-1",
            # An OpenAI-style message may carry only an image/file part.
            description=request.focus_text() or "Respond to the user's attached file(s).",
            task_type=analysis.task_types[0] if analysis.task_types else TaskType.GENERAL,
            difficulty=profile.difficulty,
            dependencies=[],
            required_context=list(request.context.keys()),
            required_capabilities=analysis.required_capabilities,
            constraints=constraints.hard_values(),
            expected_output=f"Response satisfying the original request in {constraints.constraints.output_format} format",
            goal_ids=[goal.goal_id for goal in goal_extraction.goals] if goal_extraction else [],
        )
        result = DecompositionResult(
            original_request_id=request.request_id,
            decomposition_applied=False,
            subtasks=[task],
            execution_mode="single",
            preserved_constraints=constraint_values,
            decomposition_reason=reason,
            goal_contract_version=goal_extraction.extractor_version if goal_extraction else None,
        )
        return DecompositionOutcome(result=result, strategy=self.name)
