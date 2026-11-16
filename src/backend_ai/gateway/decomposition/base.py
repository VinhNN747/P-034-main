from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from src.shared.dtos.gateway import (
    DecompositionResult,
    EffectiveConstraints,
    GatewayRequest,
    GoalExtractionResult,
    ModelUsage,
    RequestProfile,
    SemanticAnalysis,
)


@dataclass
class DecompositionOutcome:
    result: DecompositionResult
    strategy: str
    model: str | None = None
    usage: ModelUsage = field(default_factory=ModelUsage)
    fallback: bool = False
    error: str | None = None
    usage_complete: bool = True
    goal_extraction: GoalExtractionResult | None = None
    """Set when the decomposer drew its own goal boundaries (LLM planner);
    the pipeline then validates and executes against these goals instead of
    the regex extraction."""


class BaseDecomposer(ABC):
    name = "base"

    @abstractmethod
    async def decompose(
        self,
        request: GatewayRequest,
        analysis: SemanticAnalysis,
        profile: RequestProfile,
        constraints: EffectiveConstraints,
        reason: str,
        goal_extraction: GoalExtractionResult | None = None,
    ) -> DecompositionOutcome:
        raise NotImplementedError
