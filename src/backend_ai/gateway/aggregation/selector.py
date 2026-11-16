from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.backend_ai.gateway.aggregation.base import BaseAggregator

AGGREGATOR_MODES = frozenset({"auto", "none", "deterministic", "llm"})


@dataclass(frozen=True)
class AggregatorSelection:
    requested: str
    selected: str
    reason: str
    automatic: bool


def resolve_aggregator(
    requested: str,
    task_count: int,
    available: Mapping[str, BaseAggregator],
    *,
    strict_json: bool = False,
) -> AggregatorSelection:
    """Resolve an aggregation request after decomposition has produced tasks.

    The selector is deliberately independent from settings and providers. The
    registered aggregator map is the only availability signal it uses.
    Strict JSON requests may use deterministic aggregation only when that
    implementation explicitly declares JSON support. This prevents an
    offline fallback from claiming success with a Markdown answer.
    """
    if requested not in AGGREGATOR_MODES:
        raise ValueError(f"Unknown aggregator '{requested}'")
    if task_count < 1:
        raise ValueError("task_count must be at least 1")

    if requested != "auto":
        if strict_json and requested == "deterministic" and not _supports_json(available, requested):
            raise ValueError(
                "Strict JSON output requires an available LLM aggregator or a JSON-capable deterministic aggregator"
            )
        return AggregatorSelection(
            requested=requested,
            selected=requested,
            reason="explicit_selection",
            automatic=False,
        )

    if task_count == 1:
        return AggregatorSelection(
            requested=requested,
            selected="none",
            reason="single_task_passthrough",
            automatic=True,
        )

    if "llm" in available:
        return AggregatorSelection(
            requested=requested,
            selected="llm",
            reason="multiple_tasks_llm_available",
            automatic=True,
        )

    if "deterministic" in available:
        if strict_json and not _supports_json(available, "deterministic"):
            raise ValueError(
                "Strict JSON output requires an available LLM aggregator or a JSON-capable deterministic aggregator"
            )
        return AggregatorSelection(
            requested=requested,
            selected="deterministic",
            reason=("multiple_tasks_llm_unavailable_json_safe" if strict_json else "multiple_tasks_llm_unavailable"),
            automatic=True,
        )

    raise ValueError("No aggregator is available for a multi-task request")


def _supports_json(available: Mapping[str, BaseAggregator], name: str) -> bool:
    return bool(getattr(available[name], "supports_json_output", False))
