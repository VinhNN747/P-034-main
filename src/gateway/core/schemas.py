from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import to_jsonable_python


class QualityPriority(StrEnum):
    COST_FIRST = "cost_first"
    BALANCED = "balanced"
    QUALITY_FIRST = "quality_first"


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class TaskType(StrEnum):
    CUSTOMER_SUPPORT = "customer_support"
    SUMMARIZATION = "summarization"
    CONTENT_GENERATION = "content_generation"
    DATA_EXTRACTION = "data_extraction"
    REASONING = "reasoning"
    CODING = "coding"
    TRANSLATION = "translation"
    CLASSIFICATION = "classification"
    RESEARCH = "research"
    GENERAL = "general"


class PromptSegmentKind(StrEnum):
    GOAL = "goal"
    PROCESSING_STEP = "processing_step"
    OUTPUT_CONSTRAINT = "output_constraint"
    STYLE_PREFERENCE = "style_preference"


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=1)


class RequestConstraints(BaseModel):
    max_cost_usd: float | None = None
    max_latency_ms: int | None = None
    quality_priority: QualityPriority = QualityPriority.BALANCED
    allowed_models: set[str] | None = None
    blocked_models: set[str] = Field(default_factory=set)
    allowed_providers: set[str] | None = None
    blocked_providers: set[str] = Field(default_factory=set)
    output_language: str | None = None
    output_format: str = "text"
    max_output_tokens: int | None = None
    external_provider_allowed: bool = True
    allow_partial_response: bool = False

    @field_validator("output_format")
    @classmethod
    def normalize_output_format(cls, value: str) -> str:
        return value.strip().lower()


class GatewayRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    application_id: str = Field(min_length=1, max_length=128)
    prompt: str | None = None
    messages: list[ChatMessage] | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: RequestConstraints = Field(default_factory=RequestConstraints)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def prompt_or_messages(self) -> GatewayRequest:
        if self.prompt is None and not self.messages:
            raise ValueError("prompt or messages is required")
        return self

    def prompt_text(self) -> str:
        if self.prompt is not None:
            return self.prompt
        return "\n".join(message.content for message in self.messages or [])


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssue(BaseModel):
    code: str
    field: str
    message: str
    severity: IssueSeverity


class ValidationResult(BaseModel):
    accepted: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)
    estimated_input_tokens: int = 0


class SemanticAnalysis(BaseModel):
    goal_count: int = Field(ge=1, le=20)
    task_types: list[TaskType] = Field(min_length=1)
    reasoning_depth: int = Field(ge=0, le=3)
    task_independence: int = Field(ge=0, le=3)
    dependency_clarity: int = Field(ge=0, le=3)
    domain_specialization: int = Field(ge=0, le=3)
    output_complexity: int = Field(ge=0, le=3)
    ambiguity_level: int = Field(ge=0, le=3)
    required_capabilities: list[str] = Field(default_factory=list)
    explicit_constraints: dict[str, Any] = Field(default_factory=dict)
    explanations: list[str] = Field(default_factory=list)


class EvidenceSpan(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def valid_span(self) -> EvidenceSpan:
        if self.end < self.start:
            raise ValueError("evidence span end must be greater than or equal to start")
        return self


class PromptSegment(BaseModel):
    segment_id: str
    kind: PromptSegmentKind
    text: str
    normalized_text: str
    evidence: EvidenceSpan
    confidence: float = Field(ge=0, le=1)


class ExplicitGoal(BaseModel):
    goal_id: str
    text: str
    normalized_text: str
    evidence: EvidenceSpan
    confidence: float = Field(ge=0, le=1)
    independently_executable: bool = True
    depends_on_goal_ids: list[str] = Field(default_factory=list)


class GoalExtractionResult(BaseModel):
    original_prompt: str
    normalized_prompt: str
    segments: list[PromptSegment] = Field(default_factory=list)
    goals: list[ExplicitGoal] = Field(default_factory=list)
    decision: Literal["single", "multi", "unclear"]
    confidence: float = Field(ge=0, le=1)
    reason: str
    extractor_version: str


class RequestProfile(BaseModel):
    multi_task_score: float = Field(ge=0, le=1)
    complexity_score: float = Field(ge=0, le=1)
    ambiguity_score: float = Field(ge=0, le=1)
    risk_score: float = Field(ge=0, le=1)
    decomposition_score: float = Field(ge=0, le=1)
    difficulty: Difficulty
    decomposition_recommended: bool
    score_explanations: list[str] = Field(default_factory=list)


class ConstraintSource(StrEnum):
    PLATFORM_POLICY = "platform_policy"
    APPLICATION_POLICY = "application_policy"
    REQUEST_METADATA = "explicit_request_metadata"
    EXPLICIT_PROMPT = "explicit_prompt_constraint"
    INFERRED_PROMPT = "inferred_prompt_constraint"
    SYSTEM_DEFAULT = "system_default"


class ConstraintEnforcement(StrEnum):
    HARD = "hard"
    SOFT = "soft"


class ResolvedConstraint(BaseModel):
    value: Any
    source: ConstraintSource
    enforcement: ConstraintEnforcement
    confidence: float = Field(ge=0, le=1)
    contributing_sources: list[ConstraintSource] = Field(default_factory=list)


class EffectiveConstraints(BaseModel):
    constraints: RequestConstraints
    resolved: dict[str, ResolvedConstraint]
    conflicts: list[str] = Field(default_factory=list)

    def hard_values(self) -> dict[str, Any]:
        return {
            key: to_jsonable_python(item.value)
            for key, item in self.resolved.items()
            if item.enforcement == ConstraintEnforcement.HARD
        }


class SubTask(BaseModel):
    task_id: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1)
    task_type: TaskType
    difficulty: Difficulty
    dependencies: list[str] = Field(default_factory=list)
    required_context: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    expected_output: str = Field(min_length=1)
    goal_ids: list[str] = Field(default_factory=list)
    input_from: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Maps a dependency's task_id to what this task consumes from it "
            "(e.g. {'task_1': 'summary'}), so a handoff can pass just that "
            "output instead of the full upstream context."
        ),
    )


class GraphValidationStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    FALLBACK = "fallback"


class DecompositionResult(BaseModel):
    original_request_id: str
    decomposition_applied: bool
    subtasks: list[SubTask]
    execution_mode: Literal["single", "parallel", "sequential", "hybrid"]
    preserved_constraints: dict[str, Any] = Field(default_factory=dict)
    decomposition_reason: str
    validation_status: GraphValidationStatus = GraphValidationStatus.VALID
    goal_contract_version: str | None = None
    clarification_needed: bool = False


class StageTrace(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    request_id: str
    stage: str
    strategy: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    status: Literal["success", "error", "fallback", "skipped"] = "success"
    error: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class CostSummary(BaseModel):
    understand_cost: float = 0.0
    decomposition_cost: float = 0.0
    validation_cost: float = 0.0
    total_pre_routing_cost: float = 0.0
    routing_cost: float = 0.0
    aggregation_cost: float = 0.0
    total_cost: float = 0.0
    estimated: bool = False


class ExecutionResultTrace(BaseModel):
    """Pydantic mirror of ``src.backend_ai.agents.routing.router.ExecutionResult`` for
    the gateway layer (``PipelineResult``/API responses). Kept as a separate
    model instead of importing the agents-layer dataclass here: the agents
    package already imports from ``src.backend_ai.gateway.core.schemas``, so importing
    back would create a package cycle. See ``reports/AGGREGATE_TRACE_EVALUATE_DESIGN.md``
    section 5.1."""

    model_config = ConfigDict(protected_namespaces=())

    task_id: str
    status: Literal["success", "failed"]
    result: str | None = None
    selected_model: str | None = None
    executed_model: str | None = None
    fallback: bool = False
    route: str | None = None
    """The routing tier (small_fast/mid/large_reasoning). None when routing
    did not complete or execution used the fixed fallback."""
    failure_type: str | None = None
    failure_reason: str | None = None
    latency_ms: float = 0.0
    confidence: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cost_estimated: bool = False


class AggregationStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class SubtaskContribution(BaseModel):
    task_id: str
    goal_ids: list[str] = Field(default_factory=list)
    status: Literal["success", "failed"]
    used_in_final_answer: bool
    excerpt: str | None = None


class AggregationResult(BaseModel):
    original_request_id: str
    status: AggregationStatus
    final_answer: str | None = None
    contributions: list[SubtaskContribution] = Field(default_factory=list)
    missing_goal_ids: list[str] = Field(default_factory=list)
    strategy: str
    reason: str


class PipelineResult(BaseModel):
    request: GatewayRequest
    validation: ValidationResult
    goal_extraction: GoalExtractionResult | None = None
    effective_constraints: EffectiveConstraints | None = None
    semantic_analysis: SemanticAnalysis | None = None
    profile: RequestProfile | None = None
    decomposition: DecompositionResult | None = None
    execution_results: list[ExecutionResultTrace] | None = None
    aggregation: AggregationResult | None = None
    traces: list[StageTrace] = Field(default_factory=list)
    costs: CostSummary = Field(default_factory=CostSummary)


class ModelUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class StructuredModelResponse(BaseModel):
    data: dict[str, Any]
    usage: ModelUsage = Field(default_factory=ModelUsage)
    model: str
