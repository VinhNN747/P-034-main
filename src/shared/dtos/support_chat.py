from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SupportIntent = Literal[
    "faq",
    "application_list",
    "application_detail",
    "application_statistics",
    "usage",
    "analytics",
    "account",
    "billing",
    "models",
    "routing",
    "clarify",
    "blocked",
]
SupportPeriod = Literal["today", "yesterday", "this_week", "this_month", "last_7_days", "last_30_days"]
SupportChartType = Literal["line", "bar"]
SupportMetric = Literal["requests", "cost_usd", "input_tokens", "output_tokens", "tokens", "success_rate", "latency_ms"]


class SupportAnalyticsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["timeseries", "breakdown", "compare", "failures"]
    metric: SupportMetric
    group_by: Literal["day", "week", "request", "application", "model", "source"]
    chart_type: SupportChartType | None
    start_date: str | None = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class SupportDataRow(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    label: str
    value: float | None
    requests: int = 0
    detail: str | None = None


class SupportAnalyticsData(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(default_factory=lambda: str(uuid4()))
    operation: Literal["timeseries", "breakdown", "compare", "failures"]
    metric: SupportMetric
    dimension: str
    unit: str
    title: str
    scope: str
    start: str
    end: str
    timezone: str
    as_of: str
    metric_definition: str
    rows: list[SupportDataRow] = Field(max_length=100)
    total: float | None = None
    baseline: float | None = None
    delta: float | None = None
    percent_change: float | None = None
    chart_type: SupportChartType | None = None
    truncated: bool = False
    application_ids: list[str] = Field(default_factory=list, exclude=True)


class SupportConversationState(BaseModel):
    """Server-owned slots, never supplied by the browser or used as authorization."""

    model_config = ConfigDict(extra="forbid")
    intent: SupportIntent = "clarify"
    application_id: str | None = Field(default=None, max_length=100)
    application_name: str | None = Field(default=None, max_length=200)
    period: SupportPeriod = "last_7_days"
    faq_id: str | None = Field(default=None, max_length=100)
    analytics: SupportAnalyticsQuery | None = None
    pending_slot: Literal["application", "period"] | None = None


class SupportIntentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: SupportIntent
    confidence: float = Field(ge=0, le=1)
    application_name: str | None
    period: SupportPeriod | None
    follow_up: bool
    analytics: SupportAnalyticsQuery | None = None


class SupportChatContext(BaseModel):
    """Browser context is only a hint; resource access is verified server-side."""

    model_config = ConfigDict(extra="forbid")
    page: Literal[
        "workspace",
        "applications",
        "application_detail",
        "application_settings",
        "playground",
        "usage",
        "billing",
        "models",
    ] = "workspace"
    application_id: str | None = Field(default=None, min_length=1, max_length=100)


class SupportChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(..., min_length=1, max_length=5000)
    context: SupportChatContext | None = None
    session_id: str | None = Field(default=None, min_length=36, max_length=36)

    @field_validator("message")
    @classmethod
    def non_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message cannot be blank")
        return value.strip()


class SupportChatSource(BaseModel):
    id: str
    kind: Literal["faq", "documentation", "database"]
    title: str
    url: str | None = None
    as_of: str | None = None


class SupportChatAction(BaseModel):
    label: str
    url: str


class SupportChatChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=5000)


class SupportChatResponse(BaseModel):
    request_id: str
    session_id: str | None = None
    status: Literal["answered", "needs_clarification", "insufficient_evidence"]
    message: str
    sources: list[SupportChatSource] = Field(default_factory=list)
    actions: list[SupportChatAction] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    choices: list[SupportChatChoice] = Field(default_factory=list, max_length=6)
    data: SupportAnalyticsData | None = None
    conversation_state: SupportConversationState | None = Field(default=None, exclude=True)
    grounded_by_agent: bool = Field(default=False, exclude=True)


class SupportChatSession(BaseModel):
    id: str
    title: str
    pinned: bool = False
    created_at: str
    updated_at: str


class SupportChatSessionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=120)
    pinned: bool | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        title = " ".join(value.split())
        if not title:
            raise ValueError("Title cannot be blank")
        return title

    @model_validator(mode="after")
    def has_update(self):
        if self.title is None and self.pinned is None:
            raise ValueError("Provide a title or pinned state")
        return self


class SupportChatMessage(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: str
    status: str | None = None
    sources: list[SupportChatSource] = Field(default_factory=list)
    actions: list[SupportChatAction] = Field(default_factory=list)
    choices: list[SupportChatChoice] = Field(default_factory=list, max_length=6)
    data: SupportAnalyticsData | None = None


class SupportChatSessionPage(BaseModel):
    items: list[SupportChatSession]
    has_more: bool


class SupportChatSessionDetail(BaseModel):
    session: SupportChatSession
    messages: list[SupportChatMessage]
    has_more: bool
