from datetime import datetime

from pydantic import BaseModel, Field

from src.shared.dtos.gateway import QualityPriority


class GatewayConfigUpsert(BaseModel):
    model_pool_id: str | None = None
    routing_policy_id: str | None = None
    budget_ceiling: float | None = Field(default=None, ge=0)
    context_window: int | None = Field(default=None, ge=1)
    fallback_model_id: str | None = None
    # jev scores + TierPolicy (V5); falls back to rule when jev is unavailable.
    default_understander: str = "jev"
    default_decomposer: str = "deterministic"
    default_router: str = "database"
    default_aggregator: str = "deterministic"
    model_ids: list[str] = Field(default_factory=list)
    fallback_model_ids: list[str] = Field(default_factory=list)
    routing_objective: QualityPriority = QualityPriority.BALANCED
    warning_threshold: float = Field(default=0.8, ge=0, le=1)
    budget_action: str = "block"
    max_output_tokens: int = Field(default=800, ge=1)
    max_latency_ms: int | None = Field(default=None, ge=1)
    timeout_seconds: float = Field(default=30, gt=0)
    retry_limit: int = Field(default=2, ge=0, le=10)
    fallback_enabled: bool = True
    # The classifier understander's tier thresholds for this application; None = the server default.
    classifier_mid_difficulty: float | None = Field(default=None, ge=0, le=1)
    classifier_large_difficulty: float | None = Field(default=None, ge=0, le=1)
    expected_version: int | None = Field(default=None, ge=1)

class GatewayConfigRead(GatewayConfigUpsert):
    id: str
    application_id: str
    created_at: datetime
    updated_at: datetime
    version: int = 1
