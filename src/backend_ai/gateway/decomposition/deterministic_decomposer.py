from __future__ import annotations

from src.backend_ai.gateway.decomposition.base import BaseDecomposer, DecompositionOutcome
from src.backend_ai.gateway.decomposition.no_decomposer import NoDecomposer
from src.backend_ai.gateway.understanding.rules import (
    detect_task_types,
    estimate_task_difficulty,
    required_capabilities_for_prompt,
)
from src.shared.dtos.gateway import (
    DecompositionResult,
    EffectiveConstraints,
    GatewayRequest,
    GoalExtractionResult,
    RequestProfile,
    SemanticAnalysis,
    SubTask,
)


class DeterministicGoalDecomposer(BaseDecomposer):
    """Build one stable task per explicit goal without asking a model to rediscover boundaries."""

    name = "deterministic-goals"

    def __init__(self) -> None:
        self.single_fallback = NoDecomposer()

    async def decompose(
        self,
        request: GatewayRequest,
        analysis: SemanticAnalysis,
        profile: RequestProfile,
        constraints: EffectiveConstraints,
        reason: str,
        goal_extraction: GoalExtractionResult | None = None,
    ) -> DecompositionOutcome:
        if not goal_extraction or goal_extraction.decision != "multi":
            fallback = await self.single_fallback.decompose(
                request,
                analysis,
                profile,
                constraints,
                reason,
                goal_extraction,
            )
            fallback.strategy = f"{self.name}->{fallback.strategy}"
            fallback.fallback = True
            fallback.error = "Goal confidence was too low for deterministic multi-task fallback"
            return fallback

        task_by_goal = {goal.goal_id: f"task-{index}" for index, goal in enumerate(goal_extraction.goals, 1)}
        hard_constraints = constraints.hard_values()
        subtasks: list[SubTask] = []
        for goal in goal_extraction.goals:
            task_types = detect_task_types(goal.normalized_text)
            dependencies = [
                task_by_goal[dependency] for dependency in goal.depends_on_goal_ids if dependency in task_by_goal
            ]
            subtasks.append(
                SubTask(
                    task_id=task_by_goal[goal.goal_id],
                    description=goal.normalized_text,
                    task_type=task_types[0],
                    difficulty=estimate_task_difficulty(
                        goal.normalized_text,
                        fallback=profile.difficulty,
                    ),
                    dependencies=dependencies,
                    required_context=list(request.context),
                    required_capabilities=required_capabilities_for_prompt(
                        goal.normalized_text,
                        has_context=bool(request.context),
                        output_format=constraints.constraints.output_format,
                    ),
                    constraints=hard_constraints,
                    expected_output=f"Complete deliverable for {goal.goal_id}: {goal.normalized_text}",
                    goal_ids=[goal.goal_id],
                    input_from={dependency: "result" for dependency in dependencies},
                )
            )

        dependency_count = sum(bool(task.dependencies) for task in subtasks)
        if dependency_count == 0:
            execution_mode = "parallel"
        elif dependency_count == len(subtasks) - 1 and all(task.dependencies for task in subtasks[1:]):
            execution_mode = "sequential"
        else:
            execution_mode = "hybrid"

        result = DecompositionResult(
            original_request_id=request.request_id,
            decomposition_applied=True,
            subtasks=subtasks,
            execution_mode=execution_mode,
            preserved_constraints=constraints.constraints.model_dump(mode="json", exclude_none=True),
            decomposition_reason=reason,
            goal_contract_version=goal_extraction.extractor_version,
        )
        return DecompositionOutcome(result=result, strategy=self.name)
