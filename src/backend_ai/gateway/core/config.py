from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.shared.dtos.gateway import RoutingWindow


class ScoringWeights(BaseModel):
    multi_goal_count: float = 0.35
    multi_task_type_diversity: float = 0.25
    multi_task_independence: float = 0.25
    multi_dependency_clarity: float = 0.15

    complexity_reasoning_depth: float = 0.30
    complexity_goal_count: float = 0.20
    complexity_domain_specialization: float = 0.15
    complexity_output_complexity: float = 0.15
    complexity_context_size: float = 0.10
    complexity_constraint_count: float = 0.10

    decomposition_multi_task: float = 0.35
    decomposition_task_independence: float = 0.25
    decomposition_task_type_diversity: float = 0.20
    decomposition_dependency_clarity: float = 0.10
    decomposition_context_feasibility: float = 0.10


class GatewayConfig(BaseModel):
    pipeline_version: str = "goal-contract-v1"
    goal_extractor_version: str = "deterministic-goals-v2"
    explicit_goal_confidence_threshold: float = 0.85
    pipeline_cache_max_entries: int = 256
    max_prompt_chars: int = 100_000
    max_input_tokens: int = 25_000
    max_attachments: int = 10
    routing_window: RoutingWindow = Field(default_factory=RoutingWindow)
    tier_policy_high: float = Field(default=1.5, ge=0, le=3)
    tier_policy_very_high: float = Field(default=2.5, ge=0, le=3)
    tier_policy_min_confidence: float = Field(default=0.6, ge=0, le=1)
    jev_multi_task_threshold: float = Field(default=0.7, ge=0, le=1)
    jev_single_task_threshold: float = Field(default=0.3, ge=0, le=1)
    max_subtasks: int = 8
    decomposition_mode: Literal["difficulty_gap", "legacy"] = "difficulty_gap"
    """difficulty_gap: split only when parts differ greatly in difficulty (split gate, then each
    subtask's own jev tier). legacy: split whenever several deliverables are found."""
    split_gate_threshold: float = Field(default=0.5, ge=0, le=1)
    """Split gate probability at or above which boundaries are drawn and subtasks scored."""
    split_min_tier_gap: int = Field(default=2, ge=1, le=2)
    """Tier steps between the easiest and the hardest subtask needed to keep a split
    (2: small_fast and large_reasoning in one request)."""
    decomposition_threshold: float = 0.55
    semantic_check_threshold: float = 0.40
    max_ambiguity_for_decomposition: float = 0.67
    max_decomposition_overhead_ratio: float = 0.25
    assumed_decomposition_overhead_tokens: int = 700
    max_goal_count_normalization: int = 4
    context_chars_normalization: int = 20_000
    constraint_count_normalization: int = 8
    supported_output_formats: set[str] = Field(
        default_factory=lambda: {"text", "json", "markdown", "csv", "html", "xml"}
    )
    understander_model: str = "gpt-4o-mini"
    decomposer_model: str = "gpt-4o-mini"
    aggregator_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 1
    model_tier_small_fast: str = "gpt-4o-mini"
    model_tier_mid: str = "gpt-4o"
    model_tier_large_reasoning: str = "o1"
    routing_fallback_model: str = "gpt-4o-mini"
    routing_timeout_seconds: float = 30.0
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
