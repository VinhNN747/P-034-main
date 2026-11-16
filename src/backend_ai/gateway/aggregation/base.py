from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from src.shared.dtos.gateway import (
    AggregationResult,
    DecompositionResult,
    EffectiveConstraints,
    GatewayRequest,
    GoalExtractionResult,
    ModelUsage,
    OutputEvidence,
)


@runtime_checkable
class SubtaskExecutionResult(Protocol):
    """Structural type for what aggregation needs from a subtask's execution
    result. Matches ``src.backend_ai.agents.routing.router.ExecutionResult`` without
    importing the agents package from gateway (agents already imports
    gateway.core.schemas; importing back would create a package cycle and
    reverse the intended dependency direction — see
    reports/AGGREGATE_TRACE_EVALUATE_DESIGN.md section 4.1)."""

    task_id: str
    status: str  # "success" | "failed"
    result: str | None


@dataclass
class AggregationOutcome:
    result: AggregationResult
    strategy: str
    model: str | None = None
    usage: ModelUsage = field(default_factory=ModelUsage)
    fallback: bool = False
    error: str | None = None
    usage_complete: bool = True
    output_evidence: OutputEvidence = field(default_factory=OutputEvidence)


class BaseAggregator(ABC):
    name: str = "base"
    supports_json_output: bool = False

    @abstractmethod
    async def aggregate(
        self,
        request: GatewayRequest,
        decomposition: DecompositionResult,
        execution_results: list[SubtaskExecutionResult],
        constraints: EffectiveConstraints,
        goal_extraction: GoalExtractionResult | None = None,
    ) -> AggregationOutcome:
        raise NotImplementedError
