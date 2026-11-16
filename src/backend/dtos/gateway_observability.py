from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from src.backend.dtos.application_budget import ApplicationBudgetSummary


class GatewayOverviewResponse(BaseModel):
    total_requests: int = 0
    success_rate: float = 0.0
    average_latency_ms: float = 0.0
    total_cost: float = 0.0
    active_model_count: int = 0
    recent_signals: list[dict[str, Any]] = Field(default_factory=list)
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None

    model_config = {"populate_by_name": True}


class UsageRowResponse(BaseModel):
    request_id: str
    timestamp: datetime
    application_id: str
    application: str
    application_deleted: bool = False
    model: str | None = None
    provider: str | None = None
    source: str
    status: str
    tokens: int = 0
    latency_ms: float = 0.0
    cost: float = 0.0
    fallback_used: bool = False


class UsageSummaryResponse(BaseModel):
    requests: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0


class UsagePageResponse(BaseModel):
    items: list[UsageRowResponse]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total: int = Field(ge=0)
    summary: UsageSummaryResponse
    budget: ApplicationBudgetSummary | None = None
