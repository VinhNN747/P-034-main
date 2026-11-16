from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ApplicationBudgetSummary(BaseModel):
    application_id: str
    period_start: datetime
    period_end: datetime
    limit_usd: float | None = Field(default=None, ge=0)
    spent_usd: float = Field(default=0, ge=0)
    remaining_usd: float | None = None
    overage_usd: float = Field(default=0, ge=0)
    usage_percent: float | None = Field(default=None, ge=0)
    warning_threshold: float = Field(default=0.8, ge=0, le=1)
    budget_action: str = "block"
    requests: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    state: Literal["not_configured", "healthy", "warning", "exhausted"]
