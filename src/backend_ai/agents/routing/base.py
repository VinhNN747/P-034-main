from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.shared.dtos.gateway import SubTask

if TYPE_CHECKING:
    from src.backend_ai.agents.routing.router import ExecutionResult


@dataclass
class RouteDecision:
    task_id: str
    tier: str
    strategy: str
    selected_model: str | None = None
    provider_name: str | None = None
    provider_kind: str | None = None
    provider_model: str | None = None
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    pricing_version: str | None = None
    pricing_estimated: bool = False
    fallback_models: list[str] = field(default_factory=list)
    fallback_provider_names: dict[str, str] = field(default_factory=dict)
    fallback_provider_kinds: dict[str, str] = field(default_factory=dict)
    fallback_provider_models: dict[str, str] = field(default_factory=dict)
    fallback_input_prices_per_million: dict[str, float] = field(default_factory=dict)
    fallback_output_prices_per_million: dict[str, float] = field(default_factory=dict)
    fallback_pricing_estimated: dict[str, bool] = field(default_factory=dict)
    confidence: float | None = None
    reason: str | None = None
    """Why this tier/model was chosen (e.g. the TierPolicy rule that fired)."""
    shadow: dict | None = None
    """A shadow tier decision logged next to the real one (not used to route)."""
    prefetched_result: ExecutionResult | None = None


class BaseRouter(ABC):
    name = "base"

    @abstractmethod
    async def aroute(
        self,
        subtask: SubTask,
        upstream_results: dict[str, str] | None = None,
        *,
        original_prompt: str | None = None,
        request_context: dict[str, object] | None = None,
        route_hints: dict[str, object] | None = None,
        **kwargs: object,
    ) -> RouteDecision:
        """``route_hints`` carries soft preferences such as
        ``quality_priority`` (set by an OpenAI ``model`` alias like
        "auto:cheap"); routers may ignore keys they do not understand."""
        raise NotImplementedError
