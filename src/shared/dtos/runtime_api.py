from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from src.shared.dtos.gateway import ChatMessage, OutputEvidence


class RuntimeAPIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        request_id: str | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.headers = headers or {}
        self.request_id = request_id
        super().__init__(message)


class RuntimeErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class RuntimeErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: RuntimeErrorDetail
    request_id: str


async def runtime_api_error_handler(request: Request, exc: RuntimeAPIError) -> JSONResponse:
    request_id = exc.request_id or getattr(request.state, "request_id", None) or str(uuid4())
    body = RuntimeErrorEnvelope(
        error=RuntimeErrorDetail(
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            details=exc.details,
        ),
        request_id=request_id,
    ).model_dump(mode="json")
    headers = {"X-Request-ID": request_id, **exc.headers}
    if request.url.path.startswith("/api/v1/runtime/"):
        headers["X-Gateway-API-Version"] = "1"
        idempotency_key = getattr(request.state, "idempotency_key", None)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
            headers["Idempotency-Replayed"] = "false"
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


class RequestConstraints(BaseModel):
    """Public constraints accepted from an application runtime caller."""

    model_config = ConfigDict(extra="forbid")

    quality_priority: Literal["cost_first", "balanced", "latency_first", "quality_first"] | None = None
    max_output_tokens: StrictInt | None = Field(default=None, ge=1, le=32_768)
    max_latency_ms: StrictInt | None = Field(default=None, gt=0)
    allowed_models: list[StrictStr] | None = None
    blocked_models: list[StrictStr] | None = None
    allowed_providers: list[StrictStr] | None = None
    blocked_providers: list[StrictStr] | None = None
    external_provider_allowed: StrictBool | None = None
    output_format: Literal["text", "markdown", "json"] | None = None
    allow_partial_response: StrictBool | None = None


class RuntimeComparisonOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference_model_ids: list[StrictStr] = Field(min_length=1, max_length=3)
    execution_strategy: Literal["sequential", "parallel"] = "sequential"
    quality_evaluation: StrictBool = False
    evaluator_model_id: StrictStr | None = None
    max_comparison_cost_usd: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def unique_models(self):
        if len(self.reference_model_ids) != len(set(self.reference_model_ids)):
            raise ValueError("reference_model_ids must be unique")
        return self


class RuntimeRequestInput(BaseModel):
    """Exactly one of ``prompt`` (plain text) or ``messages`` (OpenAI-style
    chat messages whose content may hold image_url / file parts, including
    Files API ``file_id`` references)."""

    model_config = ConfigDict(extra="forbid")

    prompt: StrictStr | None = Field(default=None, min_length=1, max_length=100_000)
    messages: list[ChatMessage] | None = Field(default=None, min_length=1, max_length=200)
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: RequestConstraints = Field(default_factory=RequestConstraints)
    metadata: dict[str, Any] = Field(default_factory=dict)
    comparison: RuntimeComparisonOptions | None = None

    @model_validator(mode="after")
    def prompt_or_messages(self) -> RuntimeRequestInput:
        if (self.prompt is None) == (self.messages is None):
            raise ValueError("send exactly one of prompt or messages")
        if self.prompt is not None and not self.prompt.strip():
            raise ValueError("prompt must contain non-whitespace characters")
        return self


class RuntimeWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class RuntimeOperationError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class RuntimeUsageScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int
    output_tokens: int
    total_tokens: int


class RuntimeUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution: RuntimeUsageScope | None = None
    pipeline: RuntimeUsageScope | None = None
    final_output: RuntimeUsageScope | None = None
    complete: bool


class RuntimeUsageDetails(BaseModel):
    """Inclusive pipeline token-detail counters, when every model call measured them."""

    model_config = ConfigDict(extra="forbid")

    cached_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    complete: bool = False

    @model_validator(mode="after")
    def counters_match_completeness(self):
        values = (self.cached_tokens, self.cache_write_tokens, self.reasoning_tokens)
        if self.complete and any(value is None for value in values):
            raise ValueError("complete usage details require all measured counters")
        return self


@dataclass(frozen=True)
class RuntimeOutputPolicy:
    error: RuntimeOperationError | None = None


class RuntimeCost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: Literal["USD"] = "USD"
    estimated_cost: float
    estimated: bool = True


class RuntimeRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    application_id: str
    status: Literal["running", "completed", "partial", "failed", "indeterminate"]
    execution_mode: Literal["offline", "live"]
    output: str | None = None
    openai_choices: list[dict[str, Any]] | None = None
    openai_output_order: list[str] | None = None
    models_used: list[str] = Field(default_factory=list)
    providers_used: list[str] = Field(default_factory=list)
    usage: RuntimeUsage | None = None
    usage_details: RuntimeUsageDetails | None = None
    output_evidence: OutputEvidence | None = None
    cost: RuntimeCost | None = None
    latency_ms: float | None = None
    fallback_used: bool = False
    config_version: int
    started_at: datetime
    finished_at: datetime | None = None
    warnings: list[RuntimeWarning] = Field(default_factory=list)
    error: RuntimeOperationError | None = None
    comparison_id: str | None = None
