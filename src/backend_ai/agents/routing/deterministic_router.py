from __future__ import annotations

from src.backend_ai.agents.routing.base import BaseRouter, RouteDecision
from src.backend_ai.gateway.routing.tier_policy import TierPolicy
from src.shared.dtos.gateway import Difficulty, QualityPriority, SubTask, TaskType

_DIFFICULTY_TIER: dict[Difficulty, str] = {
    Difficulty.EASY: "small_fast",
    Difficulty.MEDIUM: "mid",
    Difficulty.HARD: "large_reasoning",
}
_TIER_ORDER = ["small_fast", "mid", "large_reasoning"]
_DEMANDING_TASK_TYPES = {TaskType.REASONING, TaskType.CODING, TaskType.RESEARCH}


def _shift(tier: str, steps: int) -> str:
    index = min(max(_TIER_ORDER.index(tier) + steps, 0), len(_TIER_ORDER) - 1)
    return _TIER_ORDER[index]


def select_tier(subtask: SubTask, quality_priority: QualityPriority | str | None = None) -> str:
    """Return the cheapest capable tier for the demo-compatible route.

    ``quality_priority`` (from an OpenAI ``model`` alias) nudges the result
    one tier: cost_first steps down except for hard tasks, quality_first
    steps up.
    """
    tier = _DIFFICULTY_TIER[subtask.difficulty]
    if subtask.task_type in _DEMANDING_TASK_TYPES:
        tier = _shift(tier, 1)
    if quality_priority == QualityPriority.COST_FIRST and subtask.difficulty != Difficulty.HARD:
        tier = _shift(tier, -1)
    elif quality_priority == QualityPriority.QUALITY_FIRST:
        tier = _shift(tier, 1)
    return tier


class DeterministicRouter(BaseRouter):
    """Subtasks carrying ``routing_signals`` (set by the pipeline from the
    understander's scores) are routed by ``TierPolicy``'s rule table; bare
    subtasks fall back to ``select_tier`` on their declared difficulty."""

    name = "deterministic"

    def __init__(
        self,
        model_by_tier: dict[str, str] | None = None,
        *,
        fallback_model: str | None = None,
        policy: TierPolicy | None = None,
    ) -> None:
        self._model_by_tier = model_by_tier or {}
        self._fallback_model = fallback_model
        self._policy = policy or TierPolicy()

    async def aroute(self, subtask: SubTask, upstream_results=None, *, route_hints=None, **kwargs) -> RouteDecision:
        quality_priority = (route_hints or {}).get("quality_priority")
        if subtask.routing_signals is not None:
            decision = self._policy.decide(
                subtask.routing_signals, task_type=subtask.task_type, quality_priority=quality_priority
            )
            tier, reason = decision.tier, decision.explain()
            confidence = subtask.routing_signals.confidence
        else:
            tier = select_tier(subtask, quality_priority)
            reason = f"difficulty={subtask.difficulty.value} (no routing signals)"
            confidence = 1.0
        selected = self._model_by_tier.get(tier)
        fallbacks = (
            [self._fallback_model]
            if self._fallback_model and self._fallback_model != (selected or tier)
            else []
        )
        return RouteDecision(
            task_id=subtask.task_id,
            tier=tier,
            strategy=self.name,
            selected_model=selected,
            fallback_models=fallbacks,
            confidence=confidence,
            reason=reason,
        )
