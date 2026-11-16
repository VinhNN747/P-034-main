from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_serializer, model_validator
from pydantic_core import to_jsonable_python


class QualityPriority(StrEnum):
    COST_FIRST = "cost_first"
    BALANCED = "balanced"
    LATENCY_FIRST = "latency_first"
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
    SOURCE_MATERIAL = "source_material"


class TextContentPart(BaseModel):
    """OpenAI Chat Completions ``{"type": "text", "text": ...}`` part."""

    type: Literal["text"] = "text"
    text: str


class ImageURL(BaseModel):
    url: str = Field(min_length=1, description="https URL or data:image/...;base64,... URL")
    detail: Literal["auto", "low", "high"] = "auto"


class ImageContentPart(BaseModel):
    """OpenAI Chat Completions ``{"type": "image_url", "image_url": {...}}`` part."""

    type: Literal["image_url"] = "image_url"
    image_url: ImageURL


class FileData(BaseModel):
    file_data: str | None = Field(default=None, description="data:<mime>;base64,... or raw base64")
    file_id: str | None = None
    filename: str | None = None

    @model_validator(mode="after")
    def data_or_id(self) -> FileData:
        if not self.file_data and not self.file_id:
            raise ValueError("file part requires file_data or file_id")
        return self


class FileContentPart(BaseModel):
    """OpenAI Chat Completions ``{"type": "file", "file": {...}}`` part (PDF etc.)."""

    type: Literal["file"] = "file"
    file: FileData


ContentPart = Annotated[TextContentPart | ImageContentPart | FileContentPart, Field(discriminator="type")]


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    arguments: str


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    type: Literal["function"] = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    """OpenAI-compatible chat message: ``content`` is a string or a list of
    content parts, exactly as the OpenAI SDK sends it."""

    role: Literal["system", "developer", "user", "assistant", "tool"]
    model_config = ConfigDict(extra="forbid")
    content: str | list[ContentPart] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    refusal: str | None = Field(default=None, min_length=1)

    @field_validator("content")
    @classmethod
    def non_empty(cls, value):
        if value is not None and not value:
            raise ValueError("content must not be empty")
        return value

    @model_validator(mode="after")
    def protocol_shape(self):
        if self.content is None and (self.role != "assistant" or not (self.tool_calls or self.refusal)):
            raise ValueError("null content requires assistant tool calls or refusal")
        if self.refusal is not None and self.role != 'assistant':
            raise ValueError('refusal requires assistant role')
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("tool_calls require assistant role")
        if self.role != "tool" and self.tool_call_id is not None:
            raise ValueError("tool_call_id requires tool role")
        return self

    @model_serializer(mode="wrap")
    def serialize_message(self, handler):
        value = handler(self)
        for key in ("name", "tool_calls", "tool_call_id", "refusal"):
            if value.get(key) is None:
                value.pop(key, None)
        if self.content is None:
            value["content"] = None
        return value

    def text(self) -> str:
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        return "\n".join(part.text for part in self.content if isinstance(part, TextContentPart))

    def media_parts(self) -> list[ImageContentPart | FileContentPart]:
        if self.content is None or isinstance(self.content, str):
            return []
        return [part for part in self.content if not isinstance(part, TextContentPart)]


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


_INSTRUCTION_ROLES = frozenset({"system", "developer"})


class RoutingWindow(BaseModel):
    """Which part of a multi-turn conversation drives routing decisions
    (same idea as Kong's ``message_countback``/``ignore_*_prompts``).

    Scoring the whole history makes a short follow-up ("translate it") look
    as hard as everything said before it, and lets the decomposer re-plan
    turns that were already answered.
    """

    message_countback: int = Field(default=1, ge=1, description="Latest user messages that form the focus.")
    context_messages: int = Field(default=2, ge=0, description="Earlier messages shown as reference context.")
    context_max_chars: int = Field(default=1500, ge=0)
    include_system_prompt: bool = False
    system_max_chars: int = Field(default=4000, ge=0, description="Cap on the system prompt shown to routing.")


class GatewayRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    application_id: str = Field(min_length=1, max_length=128)
    prompt: str | None = None
    messages: list[ChatMessage] | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: RequestConstraints = Field(default_factory=RequestConstraints)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Set by RequestPipeline; never client input.
    _attachment_digest: str = PrivateAttr(default="")
    _routing_window: RoutingWindow = PrivateAttr(default_factory=RoutingWindow)
    # file_id -> ResolvedFile, loaded by the API layer for Files API uploads.
    _resolved_files: dict[str, Any] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def prompt_or_messages(self) -> GatewayRequest:
        if self.prompt is None and not self.messages:
            raise ValueError("prompt or messages is required")
        return self

    def prompt_text(self) -> str:
        """All text of the request (every role). Used for size/token checks
        and cache keys, NOT for judging what is being asked; see
        ``focus_text``. Images/files are never inlined here."""
        if self.prompt is not None:
            return self.prompt
        return "\n".join(text for message in self.messages or [] if (text := message.text()))

    def has_attachments(self) -> bool:
        return any(message.media_parts() for message in self.messages or [])

    def set_attachment_digest(self, digest: str) -> None:
        self._attachment_digest = digest

    def set_resolved_files(self, files: dict[str, Any]) -> None:
        self._resolved_files = dict(files)

    def resolved_files(self) -> dict[str, Any]:
        return self._resolved_files

    def set_routing_window(self, window: RoutingWindow) -> None:
        self._routing_window = window

    # -- conversation views -------------------------------------------------

    def _split_messages(self) -> tuple[list[ChatMessage], list[ChatMessage], list[ChatMessage]]:
        """(instructions, history, focus). Focus = the latest
        ``message_countback`` user messages; history = every other
        non-instruction message, in order."""
        messages = self.messages or []
        instructions = [m for m in messages if m.role in _INSTRUCTION_ROLES]
        conversation = [m for m in messages if m.role not in _INSTRUCTION_ROLES]
        user_indexes = [index for index, m in enumerate(conversation) if m.role == "user"]
        if user_indexes:
            focus_indexes = set(user_indexes[-self._routing_window.message_countback :])
        else:
            focus_indexes = {len(conversation) - 1} if conversation else set()
        focus = [m for index, m in enumerate(conversation) if index in focus_indexes]
        history = [m for index, m in enumerate(conversation) if index not in focus_indexes]
        return instructions, history, focus

    def focus_text(self) -> str:
        """What is being asked right now: the routing focus."""
        if self.prompt is not None:
            return self.prompt
        _, _, focus = self._split_messages()
        return "\n".join(text for message in focus if (text := message.text()))

    def system_text(self) -> str:
        if self.prompt is not None:
            return ""
        instructions, _, _ = self._split_messages()
        return "\n".join(text for message in instructions if (text := message.text()))

    def context_text(self) -> str:
        """The last ``context_messages`` history messages, role-labelled and
        capped, so a router can resolve references like "translate it"."""
        window = self._routing_window
        if self.prompt is not None or window.context_messages == 0 or window.context_max_chars == 0:
            return ""
        _, history, _ = self._split_messages()
        selected = history[-window.context_messages :]
        per_message = max(1, window.context_max_chars // max(1, len(selected)))
        lines = []
        for message in selected:
            text = message.text()
            if not text and message.media_parts():
                text = f"[{len(message.media_parts())} attachment(s)]"
            if len(text) > per_message:
                text = text[:per_message] + " [...]"
            lines.append(f"{message.role}: {text}")
        return "\n".join(lines)

    def conversation_text(self, *, max_chars: int = 20_000) -> str:
        """Instructions + full history (everything except the focus) for the
        executing model, which otherwise only sees its subtask. Keeps the
        most recent messages when over budget."""
        if self.prompt is not None:
            return ""
        instructions, history, _ = self._split_messages()
        system = "\n".join(text for message in instructions if (text := message.text()))
        lines = [f"{m.role}: {m.text() or f'[{len(m.media_parts())} attachment(s)]'}" for m in history]
        budget = max_chars - len(system)
        kept: list[str] = []
        for line in reversed(lines):
            if budget - len(line) < 0:
                kept.append("[earlier messages omitted]")
                break
            kept.append(line)
            budget -= len(line)
        sections = []
        if system:
            sections.append(f"System instructions:\n{system}")
        if kept:
            sections.append("Earlier conversation:\n" + "\n".join(reversed(kept)))
        return "\n\n".join(sections)

    def constraint_text(self) -> str:
        """Text constraints may legitimately come from: the system prompt
        ("always answer in JSON") and the current ask, not old turns."""
        return "\n".join(text for text in (self.system_text(), self.focus_text()) if text)

    def routing_text(self) -> str:
        """What text-only routing/understanding models (jev, LLM
        understander/decomposer) read: the focus, labelled context, optional
        system prompt and a short digest of analyzed attachments."""
        sections = [self.focus_text()]
        context = self.context_text()
        if context:
            sections.append(
                "<conversation_context note=\"Earlier turns, for resolving references only. "
                "Judge only the latest request above.\">\n" + context + "\n</conversation_context>"
            )
        window = self._routing_window
        if window.include_system_prompt and window.system_max_chars and (system := self.system_text()):
            # The system prompt often carries the documents a RAG app answers from; without
            # it jev cannot tell that the facts are already provided.
            if len(system) > window.system_max_chars:
                system = system[: window.system_max_chars] + f" [... {len(system) - window.system_max_chars} chars]"
            sections.append(f"<system_prompt>\n{system}\n</system_prompt>")
        if self._attachment_digest:
            sections.append(self._attachment_digest)
        return "\n\n".join(section for section in sections if section)


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


class RoutingSignals(BaseModel):
    """Per-request (or per-subtask) scores the tier policy routes on.

    All dimensions use the same 0..3 scale. jev returns expected values, so
    they may be fractional (e.g. 1.7); rule/LLM understanders give integers.
    """

    reasoning: float = Field(ge=0, le=3)
    domain: float = Field(ge=0, le=3)
    ambiguity: float = Field(ge=0, le=3)
    risk: float = Field(ge=0, le=3)
    output: float = Field(ge=0, le=3)
    knowledge_rarity: float = Field(default=0, ge=0, le=3)
    """How obscure the facts the answer depends on are (jev only; 0 for rule
    scores): a short question can hinge on a fact small models do not know."""
    confidence: float | None = Field(default=None, ge=0, le=1)
    """Lowest certainty among the scores; None when the source has none."""
    source: str = "rule"
    difficulty: float | None = Field(default=None, ge=0, le=1)
    """The local task classifier's soft difficulty: the expected share of small (7B) models
    that answer wrong. Set only when ``source == "classifier"``, where it decides the tier
    instead of the scored dimensions; None = not measured."""
    input_chars: int = Field(default=0, ge=0)
    """Characters of material to read for the latest request: its text plus
    extracted attachment text (not earlier turns). Measured, not scored."""
    context_chars: int = Field(default=0, ge=0)
    """Characters the executing model will be sent: system prompt and earlier
    turns as the executor passes them, the latest request and the attachment
    text, with the executor's own caps applied. Used to keep models whose
    context window is too small out of the candidates; not a difficulty signal."""
    live_data: float = Field(default=0, ge=0, le=1)
    """jev's probability that a good answer needs current data no model has
    (prices, quotes, news); 0 for rule scores."""
    case_specific: float | None = Field(default=None, ge=0, le=1)
    """jev's probability that the user wants a conclusion or calculation applied
    to their own case in health, law, money or safety (this patient's dose,
    this clause, this transaction's tax), not general information on such a
    topic. None = not measured (rule scores, older jev): the risk rules then
    behave as before."""
    competition: float | None = Field(default=None, ge=0, le=1)
    """jev's probability of a competition/olympiad problem, a formal proof or a
    contest-level algorithm. None = not measured."""
    answer_in_context: float | None = Field(default=None, ge=0, le=1)
    """jev's probability that the facts needed for the answer are already in the
    request, system prompt, conversation or attachments, so the task is mainly to
    look them up or apply them (RAG documents, policies, manuals). None = not
    measured."""
    small_model_trap: float | None = Field(default=None, ge=0, le=1)
    """jev's probability of a task small models are known to get wrong despite
    looking easy (counting characters, exact multi-digit arithmetic, decimal
    comparison, rhymed verse translation, exact citations, very recent tech).
    None = not measured."""
    visual_attachments: int = Field(default=0, ge=0)
    """Attachments whose meaning is mostly visual (chart, diagram, photo,
    handwriting): a text summary undersells their difficulty."""
    unreadable_attachments: int = Field(default=0, ge=0)
    """Attachments OCR could not read (failed, blocked or not configured)."""


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
    risk_level: int = Field(default=0, ge=0, le=3)
    multi_task_probability: float | None = Field(default=None, ge=0, le=1)
    """jev's probability that the latest request holds several separate
    deliverables; None when no classifier scored it (rule/LLM)."""
    routing_signals: RoutingSignals | None = None
    """Exact scores when the understander has finer ones than the integer
    fields (jev); otherwise derived from the integer fields."""

    def signals(self) -> RoutingSignals:
        if self.routing_signals is not None:
            return self.routing_signals
        return RoutingSignals(
            reasoning=self.reasoning_depth,
            domain=self.domain_specialization,
            ambiguity=self.ambiguity_level,
            risk=self.risk_level,
            output=self.output_complexity,
        )


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
    source_material: str | None = None
    """Passage/data the tasks operate on (e.g. text after "Đoạn văn:"); every
    subtask needs it, so it is kept out of goal text and shared at execution."""
    declared_goal_count: int | None = None
    """Task count the user stated ("3 nhiệm vụ", "the following 2 tasks")."""


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
        # Sets (allowed_models, blocked_providers...) are sorted: two equal sets
        # can iterate in different orders, and the decomposition validator
        # compares these lists for equality, which made it randomly reject
        # valid graphs (HARD_CONSTRAINT_NOT_PRESERVED) and fall back.
        return {
            key: to_jsonable_python(
                sorted(item.value, key=str) if isinstance(item.value, (set, frozenset)) else item.value
            )
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
    routing_signals: RoutingSignals | None = None
    """Set by the pipeline after decomposition; drives TierPolicy routing."""


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


class OutputEvidence(BaseModel):
    """Measured output/usage facts returned by the producer for a model call.

    Missing counters stay ``None`` so public adapters never manufacture usage
    detail values that the upstream provider did not report.
    """

    model_config = ConfigDict(extra="forbid")

    finish_reason: Literal["stop", "length", "content_filter", "tool_calls", "unknown"] = "unknown"
    cached_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cache_write_5m_tokens: int | None = Field(default=None, ge=0)
    cache_write_1h_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class CostSummary(BaseModel):
    attachment_cost: float = 0.0
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
    package already imports from ``src.shared.dtos.gateway``, so importing
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
    provider: str | None = None
    usage_complete: bool = False
    final_input_tokens: int | None = None
    final_output_tokens: int | None = None
    final_usage_complete: bool | None = None
    circuit_skipped: list[str] = Field(default_factory=list)
    route_reason: str | None = None
    route_shadow: dict[str, Any] | None = None
    output_evidence: OutputEvidence | None = None


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
    output_evidence: OutputEvidence | None = None


class PipelineResult(BaseModel):
    openai_choices: list[dict[str, Any]] | None = None
    request: GatewayRequest
    validation: ValidationResult
    goal_extraction: GoalExtractionResult | None = None
    effective_constraints: EffectiveConstraints | None = None
    semantic_analysis: SemanticAnalysis | None = None
    profile: RequestProfile | None = None
    decomposition: DecompositionResult | None = None
    attachments: list[AttachmentAnalysis] = Field(default_factory=list)
    execution_results: list[ExecutionResultTrace] | None = None
    aggregation: AggregationResult | None = None
    traces: list[StageTrace] = Field(default_factory=list)
    costs: CostSummary = Field(default_factory=CostSummary)
    output_evidence: OutputEvidence | None = None


class ModelUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class StructuredModelResponse(BaseModel):
    data: dict[str, Any]
    usage: ModelUsage = Field(default_factory=ModelUsage)
    model: str
    usage_complete: bool = False
    output_evidence: OutputEvidence = Field(default_factory=OutputEvidence)


class AttachmentKind(StrEnum):
    IMAGE = "image"
    PDF = "pdf"
    OTHER = "other"


class AttachmentAnalysis(BaseModel):
    """What the gateway learned about one image/file content part.

    ``extracted_text``/``description`` are a text surrogate so text-only
    stages (jev, understanders, decomposer, text-only executors) can reason
    about the attachment. They are untrusted data from the user's file, never
    instructions.
    """

    attachment_id: str
    message_index: int
    part_index: int
    kind: AttachmentKind
    source: Literal["data", "url", "file_id"]
    mime_type: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    page_count: int | None = None
    status: Literal["analyzed", "skipped", "failed"]
    method: Literal["ocr", "pdf_text_layer", "text_decode", "none"] = "none"
    content_category: str | None = None
    description: str | None = None
    extracted_text: str = ""
    language: str | None = None
    model: str | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    usage_complete: bool = False
    cache_hit: bool = False
    error: str | None = None
