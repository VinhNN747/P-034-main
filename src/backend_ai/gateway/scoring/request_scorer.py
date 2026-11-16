from __future__ import annotations

from src.backend_ai.gateway.core.config import GatewayConfig
from src.shared.dtos.gateway import (
    ConstraintSource,
    Difficulty,
    EffectiveConstraints,
    GatewayRequest,
    RequestProfile,
    SemanticAnalysis,
)


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)


class RequestScorer:
    def __init__(self, config: GatewayConfig | None = None) -> None:
        self.config = config or GatewayConfig()

    def score(
        self,
        request: GatewayRequest,
        analysis: SemanticAnalysis,
        constraints: EffectiveConstraints,
    ) -> RequestProfile:
        cfg = self.config
        w = cfg.weights
        normalized_goals = min(analysis.goal_count, cfg.max_goal_count_normalization) / cfg.max_goal_count_normalization
        task_type_diversity = (
            min(len(set(analysis.task_types)), cfg.max_goal_count_normalization) / cfg.max_goal_count_normalization
        )
        independence = analysis.task_independence / 3
        dependency_clarity = analysis.dependency_clarity / 3
        context_size = min(len(str(request.context)), cfg.context_chars_normalization) / cfg.context_chars_normalization
        explicit_constraint_count = sum(
            any(source != ConstraintSource.SYSTEM_DEFAULT for source in item.contributing_sources)
            for item in constraints.resolved.values()
        )
        constraint_count = (
            min(explicit_constraint_count, cfg.constraint_count_normalization) / cfg.constraint_count_normalization
        )
        context_feasibility = 1.0 if len(str(request.context)) <= cfg.context_chars_normalization else 0.0

        multi_task = _clamp(
            w.multi_goal_count * normalized_goals
            + w.multi_task_type_diversity * task_type_diversity
            + w.multi_task_independence * independence
            + w.multi_dependency_clarity * dependency_clarity
        )
        complexity = _clamp(
            w.complexity_reasoning_depth * (analysis.reasoning_depth / 3)
            + w.complexity_goal_count * normalized_goals
            + w.complexity_domain_specialization * (analysis.domain_specialization / 3)
            + w.complexity_output_complexity * (analysis.output_complexity / 3)
            + w.complexity_context_size * context_size
            + w.complexity_constraint_count * constraint_count
        )
        ambiguity = _clamp(analysis.ambiguity_level / 3)
        decomposition = _clamp(
            w.decomposition_multi_task * multi_task
            + w.decomposition_task_independence * independence
            + w.decomposition_task_type_diversity * task_type_diversity
            + w.decomposition_dependency_clarity * dependency_clarity
            + w.decomposition_context_feasibility * context_feasibility
        )
        risk = _clamp(
            0.4 * ambiguity
            + 0.2 * (analysis.domain_specialization / 3)
            + 0.2 * (analysis.output_complexity / 3)
            + 0.2 * (not constraints.constraints.external_provider_allowed)
        )
        difficulty = (
            Difficulty.EASY if complexity < 0.35 else Difficulty.MEDIUM if complexity < 0.70 else Difficulty.HARD
        )

        return RequestProfile(
            multi_task_score=multi_task,
            complexity_score=complexity,
            ambiguity_score=ambiguity,
            risk_score=risk,
            decomposition_score=decomposition,
            difficulty=difficulty,
            decomposition_recommended=decomposition >= cfg.decomposition_threshold,
            score_explanations=[
                f"Normalized goals={normalized_goals:.2f}, task-type diversity={task_type_diversity:.2f}.",
                f"Context preservation feasibility={context_feasibility:.2f}.",
                "All aggregate scores were calculated in code from rubric features.",
            ],
        )
