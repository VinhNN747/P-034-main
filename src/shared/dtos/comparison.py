from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_RUBRIC = {
    "version": "default-v1",
    "criteria": {
        "correctness": 40,
        "completeness": 30,
        "constraint_adherence": 20,
        "clarity": 10,
    },
}


class ComparisonCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(min_length=1, max_length=128)
    gateway_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    execution_strategy: Literal["sequential", "parallel"] = "sequential"
    prompt: str = Field(min_length=1, max_length=100_000)
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    reference_model_ids: list[str] = Field(min_length=1, max_length=3)
    quality_evaluation: bool = False
    evaluator_model_id: str | None = None
    rubric: dict[str, Any] = Field(default_factory=lambda: dict(DEFAULT_RUBRIC))
    max_comparison_cost_usd: float | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_input(self):
        if not self.prompt.strip():
            raise ValueError("prompt must contain non-whitespace characters")
        if len(self.reference_model_ids) != len(set(self.reference_model_ids)):
            raise ValueError("reference_model_ids must be unique")
        return self


class ComparisonAccepted(BaseModel):
    comparison_id: str
    status: Literal["queued", "running", "completed", "partial", "failed", "indeterminate"]
    replayed: bool = False


class ComparisonBranchRead(BaseModel):
    id: str
    branch_key: str
    branch_type: Literal["gateway", "reference"]
    model_id: str | None = None
    model_name: str | None = None
    provider_name: str | None = None
    status: str
    output: str | None = None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float
    usage_complete: bool
    pricing_estimated: bool
    latency_ms: float | None = None
    queue_delay_ms: float | None = None
    quality_score: float | None = None
    error_code: str | None = None
    error_message: str | None = None


class ComparisonDelta(BaseModel):
    reference_branch_id: str
    reference_model_id: str | None = None
    cost_delta_usd: float | None = None
    cost_delta_percent: float | None = None
    latency_delta_ms: float | None = None
    latency_delta_percent: float | None = None
    quality_delta_points: float | None = None
    comparable_cost: bool
    comparable_latency: bool
    comparable_quality: bool


class ComparisonRead(BaseModel):
    id: str
    application_id: str
    gateway_request_id: str | None = None
    source: str
    status: str
    execution_mode: str
    prompt: str
    quality_evaluation: bool
    execution_strategy: Literal["sequential", "parallel"] = "sequential"
    evaluator_model_id: str | None = None
    evaluation_status: str | None = None
    evaluator_model_name: str | None = None
    evaluation_error_code: str | None = None
    evaluation_total_tokens: int = 0
    evaluation_error_message: str | None = None
    evaluation_cost_usd: float
    evaluation_usage_complete: bool
    comparison_spend_usd: float
    branches: list[ComparisonBranchRead]
    deltas: list[ComparisonDelta]
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None


class ComparisonPage(BaseModel):
    items: list[ComparisonRead]
    page: int
    page_size: int
    total: int
