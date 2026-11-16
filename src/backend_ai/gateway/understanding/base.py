from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from src.shared.dtos.gateway import EffectiveConstraints, GatewayRequest, ModelUsage, SemanticAnalysis


@dataclass
class UnderstandingOutcome:
    analysis: SemanticAnalysis
    strategy: str
    model: str | None = None
    usage: ModelUsage = field(default_factory=ModelUsage)
    fallback: bool = False
    error: str | None = None
    usage_complete: bool = True
    details: dict[str, object] | None = None
    """Extra facts for the understand trace (e.g. the task classifier's raw output)."""


class BaseUnderstander(ABC):
    name = "base"

    @abstractmethod
    async def understand(self, request: GatewayRequest, constraints: EffectiveConstraints) -> UnderstandingOutcome:
        raise NotImplementedError
