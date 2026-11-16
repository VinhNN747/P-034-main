from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PolicyReviewStatus = Literal["pending", "approved", "rejected", "changes_requested"]
PolicyDecision = Literal["approve", "reject", "request_changes"]


class PolicyDecisionCreate(BaseModel):
    decision: PolicyDecision
    comment: str = Field(default="", max_length=4000)


class PolicyDecisionRead(BaseModel):
    id: str
    actor_user_id: str
    decision: PolicyDecision
    comment: str
    created_at: datetime


class PolicyReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    scope: str
    type: str
    submitted_by: str
    submitted_at: datetime
    status: PolicyReviewStatus
    decision_history: list[PolicyDecisionRead]
    affected_models: list[str]
    affected_applications: list[str]
    application_id: str | None = None


class PolicyReviewPage(BaseModel):
    items: list[PolicyReviewRead]
    page: int
    page_size: int
    total: int
