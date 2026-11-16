from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

IncidentSeverity = Literal["low", "medium", "high", "critical"]
IncidentStatus = Literal["investigating", "monitoring", "resolved"]
IncidentReportType = Literal["application", "model", "logic", "fallback", "other"]


class IncidentReportCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10000)
    type: IncidentReportType = "application"
    application_id: str | None = Field(default=None, min_length=1, max_length=36)
    provider: str | None = Field(default=None, min_length=1, max_length=100)
    model: str | None = Field(default=None, min_length=1, max_length=150)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    steps_to_reproduce: str = Field(default="", max_length=5000)
    expected_behavior: str = Field(default="", max_length=5000)


class IncidentCreate(BaseModel):
    application_id: str | None = None
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    severity: IncidentSeverity | Literal["info", "warning", "error"] = "medium"
    status: IncidentStatus | Literal["open", "acknowledged"] = "investigating"
    owner: str = "Unassigned"
    type: str = Field(default="operational", min_length=1, max_length=50)
    scope: str = "Gateway"
    impact: str = "Unknown"
    provider: str | None = None
    model: str | None = None
    metadata: dict = Field(default_factory=dict)


class IncidentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: IncidentStatus | Literal["open", "acknowledged"] | None = None
    severity: IncidentSeverity | Literal["info", "warning", "error"] | None = None
    owner: str | None = None
    type: str | None = Field(default=None, min_length=1, max_length=50)
    scope: str | None = None
    impact: str | None = None
    resolution_note: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def reject_explicit_nulls(self) -> IncidentUpdate:
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Incident update fields cannot be null")
        return self


class IncidentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str
    severity: IncidentSeverity
    status: IncidentStatus
    owner: str
    type: str
    scope: str
    opened_at: datetime
    duration_seconds: float
    impact: str
    application_id: str | None = None
    provider: str | None = None
    model: str | None = None
    updated_at: datetime
    source: Literal["operational", "user_report"] = "operational"
    reporter_user_id: str | None = None
    reporter_email: str | None = None
    request_id: str | None = None
    steps_to_reproduce: str = ""
    expected_behavior: str = ""
    resolution_note: str = ""


class IncidentPage(BaseModel):
    items: list[IncidentRead]
    page: int
    page_size: int
    total: int
