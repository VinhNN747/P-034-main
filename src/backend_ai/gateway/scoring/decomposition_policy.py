from __future__ import annotations

from dataclasses import dataclass

from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.understanding.goal_extractor import has_compound_action
from src.shared.dtos.gateway import (
    EffectiveConstraints,
    GatewayRequest,
    GoalExtractionResult,
    RequestProfile,
    SemanticAnalysis,
)


@dataclass(frozen=True)
class DecompositionDecision:
    should_decompose: bool
    reason: str
    band: str
    estimated_overhead_ratio: float


class DecompositionPolicy:
    def __init__(self, config: GatewayConfig | None = None) -> None:
        self.config = config or GatewayConfig()

    def decide(
        self,
        request: GatewayRequest,
        profile: RequestProfile,
        estimated_input_tokens: int,
        constraints: EffectiveConstraints | None = None,
        goal_extraction: GoalExtractionResult | None = None,
        analysis: SemanticAnalysis | None = None,
        *,
        planner_available: bool = False,
        split_gap_probability: float | None = None,
    ) -> DecompositionDecision:
        """Order: guardrails, deterministic goal extraction (single/multi),
        LLM planner (when available, it decides unclear and compound-single
        requests itself), ambiguity guardrail, jev's multi-task probability
        (only when goal extraction is unclear), then the weighted
        decomposition score.

        With the planner, regex extraction is only a cheap gate: a single goal
        with one action verb skips the planner call, anything else is planned."""
        cfg = self.config
        max_output = (
            (constraints.constraints.max_output_tokens if constraints else None)
            or request.constraints.max_output_tokens
            or 2_000
        )
        denominator = max(estimated_input_tokens + max_output, 4_000)
        overhead_ratio = cfg.assumed_decomposition_overhead_tokens / denominator

        if len(str(request.context)) > cfg.context_chars_normalization:
            return DecompositionDecision(
                False, "Context cannot be preserved safely across subtasks", "guardrail", overhead_ratio
            )
        if overhead_ratio > cfg.max_decomposition_overhead_ratio:
            return DecompositionDecision(
                False, "Estimated decomposition overhead exceeds budget ratio", "guardrail", overhead_ratio
            )
        if cfg.decomposition_mode == "difficulty_gap":
            return self._decide_by_difficulty_gap(
                goal_extraction, split_gap_probability, overhead_ratio, planner_available=planner_available
            )
        if (
            planner_available
            and goal_extraction
            and goal_extraction.decision == "single"
            and has_compound_action(goal_extraction.goals[0].normalized_text)
        ):
            return DecompositionDecision(
                True,
                "Single extracted goal names several actions; LLM planner decides the boundaries",
                "llm-planner",
                overhead_ratio,
            )
        if goal_extraction and goal_extraction.decision == "single":
            return DecompositionDecision(
                False,
                "Deterministic goal extraction found exactly one clear deliverable",
                "deterministic-single",
                overhead_ratio,
            )
        if goal_extraction and goal_extraction.decision == "multi":
            return DecompositionDecision(
                True,
                "Deterministic goal extraction found multiple clear deliverables",
                "deterministic-multi",
                overhead_ratio,
            )
        if (
            planner_available
            and goal_extraction is not None
            and goal_extraction.decision == "unclear"
            # One candidate (often the whole prompt: a question with no action verb, e.g. a word
            # problem) is not worth a planner call; jev's multi-task probability below still
            # sends it to the planner when it sees several deliverables.
            and (len(goal_extraction.goals) >= 2 or goal_extraction.declared_goal_count is not None)
        ):
            return DecompositionDecision(
                True,
                "Goal boundaries are unclear to the regex pass; LLM planner decides them",
                "llm-planner",
                overhead_ratio,
            )
        if profile.ambiguity_score >= cfg.max_ambiguity_for_decomposition:
            return DecompositionDecision(
                False, "Ambiguity guardrail blocked decomposition", "guardrail", overhead_ratio
            )
        probability = analysis.multi_task_probability if analysis is not None else None
        if probability is not None and (goal_extraction is None or goal_extraction.decision == "unclear"):
            if probability >= cfg.jev_multi_task_threshold:
                return DecompositionDecision(
                    True,
                    f"jev multi-task probability {probability:.2f} >= {cfg.jev_multi_task_threshold}",
                    "jev-multi",
                    overhead_ratio,
                )
            if probability <= cfg.jev_single_task_threshold:
                return DecompositionDecision(
                    False,
                    f"jev multi-task probability {probability:.2f} <= {cfg.jev_single_task_threshold}",
                    "jev-single",
                    overhead_ratio,
                )
        if profile.decomposition_score >= cfg.decomposition_threshold:
            return DecompositionDecision(
                True, "Decomposition score reached the configured threshold", "high", overhead_ratio
            )
        if profile.decomposition_score >= cfg.semantic_check_threshold:
            return DecompositionDecision(
                False,
                "Score is in the review band; conservative offline policy keeps a single task",
                "review",
                overhead_ratio,
            )
        return DecompositionDecision(False, "Decomposition score is below threshold", "low", overhead_ratio)

    def _decide_by_difficulty_gap(
        self,
        goal_extraction: GoalExtractionResult | None,
        probability: float | None,
        overhead_ratio: float,
        *,
        planner_available: bool,
    ) -> DecompositionDecision:
        """Split only when the split gate sees parts of very different difficulty. A yes here
        only draws the boundaries: the pipeline then scores each subtask with jev and folds the
        split back into one task unless their tiers span ``split_min_tier_gap`` steps."""
        threshold = self.config.split_gate_threshold
        if probability is None:
            return DecompositionDecision(
                False, "Split gate did not answer; the request runs as one task", "gap-unavailable", overhead_ratio
            )
        gate = f"split gate difficulty-gap probability {probability:.2f}"
        if probability < threshold:
            return DecompositionDecision(
                False, f"{gate} < {threshold}: parts have similar difficulty, one task", "gap-no", overhead_ratio
            )
        if planner_available:
            return DecompositionDecision(
                True, f"{gate} >= {threshold}; LLM planner draws the boundaries", "gap-planner", overhead_ratio
            )
        if goal_extraction is not None and goal_extraction.decision == "multi":
            return DecompositionDecision(
                True, f"{gate} >= {threshold}; regex goals are the boundaries", "gap-deterministic", overhead_ratio
            )
        if (
            goal_extraction is not None
            and goal_extraction.decision == "unclear"
            and _usable_candidates(goal_extraction, self.config.max_subtasks)
        ):
            return DecompositionDecision(
                True,
                f"{gate} >= {threshold}; regex candidate goals are the boundaries",
                "gap-candidates",
                overhead_ratio,
            )
        return DecompositionDecision(
            False,
            f"{gate} >= {threshold} but no planner and no usable goal boundaries",
            "gap-no-boundaries",
            overhead_ratio,
        )


def _usable_candidates(goal_extraction: GoalExtractionResult, max_subtasks: int) -> bool:
    goals = goal_extraction.goals
    return 2 <= len(goals) <= max_subtasks and all(goal.independently_executable for goal in goals)


def promote_goals_confirmed_by_jev(
    goal_extraction: GoalExtractionResult | None,
    decision: DecompositionDecision,
    max_subtasks: int,
) -> GoalExtractionResult | None:
    """When jev decided "several deliverables" for an *unclear* extraction,
    reuse the candidate goals the extractor already found (e.g. two clauses
    joined by "and"/"và", left just under the confidence bar) as the task
    boundaries, so the deterministic decomposer can split without an LLM.

    Returns the promoted extraction, or None when the candidates are not
    usable boundaries (fewer than 2, too many, or not independently
    executable); the decomposer then needs the LLM decomposer or stays single.
    """
    if (
        decision.band not in {"jev-multi", "gap-candidates"}
        or goal_extraction is None
        or goal_extraction.decision != "unclear"
    ):
        return None
    if not _usable_candidates(goal_extraction, max_subtasks):
        return None
    return goal_extraction.model_copy(
        update={
            "decision": "multi",
            "reason": f"{goal_extraction.reason}; boundaries confirmed by jev ({decision.reason})",
        }
    )
