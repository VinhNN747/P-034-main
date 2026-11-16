"""Contracts between management and AI; no database or provider secrets."""

from pydantic import BaseModel, ConfigDict, Field

from src.shared.dtos.support_chat import (
    SupportAnalyticsQuery,
    SupportChatChoice,
    SupportChatMessage,
    SupportChatResponse,
    SupportIntent,
    SupportIntentPlan,
    SupportPeriod,
)
from src.shared.services.support_navigation import SupportNavigationId


class SupportSynthesisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=5000)
    response: SupportChatResponse
    history: list[SupportChatMessage] = Field(default_factory=list, max_length=64)


class SupportRetrievalDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=6000)
    lexical_score: float = Field(ge=0)


class SupportUnderstandingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=5000)
    previous_intent: SupportIntent = "clarify"
    pending_slot: str | None = None
    previous_period: SupportPeriod = "last_7_days"
    previous_application_name: str | None = Field(default=None, max_length=200)
    previous_analytics: SupportAnalyticsQuery | None = None
    history: list[SupportChatMessage] = Field(default_factory=list, max_length=64)
    documents: list[SupportRetrievalDocument] = Field(min_length=1, max_length=400)


class SupportPlannerOutput(SupportIntentPlan):
    # Required nullable field keeps the provider JSON schema strict while the public plan stays backward compatible.
    analytics: SupportAnalyticsQuery | None
    retrieval_query: str | None = Field(max_length=5000)


class SupportFAQAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=6000)
    source_ids: list[str] = Field(min_length=1, max_length=6)
    navigation_ids: list[SupportNavigationId] = Field(default_factory=list, max_length=4)


class SupportFAQAgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supported: bool
    message: str = Field(max_length=6000)
    source_ids: list[str] = Field(max_length=6)
    navigation_ids: list[SupportNavigationId] = Field(max_length=4)


class SupportClarificationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    needed: bool
    message: str = Field(max_length=1200)
    choices: list[SupportChatChoice] = Field(max_length=4)


class SupportUnderstandingOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: SupportIntentPlan | None = None
    semantic_scores: dict[str, float] = Field(default_factory=dict)
    faq_id: str | None = Field(default=None, max_length=100)
    faq_answer: SupportFAQAnswer | None = None
    clarification: SupportClarificationDecision | None = None


class SupportFAQSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    faq_id: str | None
    confidence: float = Field(ge=0, le=1)


class SupportKnowledgeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_ids: list[str] = Field(max_length=12)


class SupportRewriteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=6000)
    source_ids: list[str] = Field(max_length=10)


class SupportGroundingVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supported: bool


class SupportFAQReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supported: bool
    issues: list[str] = Field(max_length=6)


class SupportSynthesisOutput(BaseModel):
    message: str = Field(min_length=1, max_length=6000)


class ModelProbeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
