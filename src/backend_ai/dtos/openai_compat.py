"""OpenAI Chat Completions wire format, as sent by the OpenAI SDK.

Unknown request fields are validated against the explicit profile. Gateway
specific options travel in ``extra_body={"gateway": {...}}``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from src.shared.dtos.gateway import ChatMessage


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_usage: bool = False


class GatewayOptions(BaseModel):
    """Gateway knobs OpenAI's schema has no field for. Mirrors the public
    runtime constraints; budget comes from the application config."""

    model_config = ConfigDict(extra="forbid")

    max_latency_ms: int | None = Field(default=None, gt=0, strict=True)
    allowed_providers: list[str] | None = None
    blocked_providers: list[str] | None = None
    blocked_models: list[str] | None = None
    external_provider_allowed: bool | None = None
    allow_partial_response: bool | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: int | None = Field(default=None, gt=0, strict=True)
    max_completion_tokens: int | None = Field(default=None, gt=0, strict=True)
    response_format: dict[str, Any] | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None
    n: int = Field(default=1, ge=1, le=4, strict=True)
    temperature: float | None = Field(default=None, ge=0, le=2, strict=True, allow_inf_nan=False)
    top_p: float | None = Field(default=None, ge=0, le=1, strict=True, allow_inf_nan=False)
    seed: int | None = Field(default=None, strict=True)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2, strict=True, allow_inf_nan=False)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2, strict=True, allow_inf_nan=False)
    parallel_tool_calls: bool | None = Field(default=None, strict=True)
    stop: StrictStr | list[StrictStr] | None = None
    user: str | None = None
    metadata: dict[str, str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    gateway: GatewayOptions = Field(default_factory=GatewayOptions)

    @field_validator("stop", mode="before")
    @classmethod
    def validate_stop_shape(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            if not value:
                raise ValueError("stop must be a non-empty string or a list of 1 to 4 non-empty strings")
            return value
        if (
            not isinstance(value, list)
            or not 1 <= len(value) <= 4
            or any(type(item) is not str or not item for item in value)
        ):
            raise ValueError("stop must be a non-empty string or a list of 1 to 4 non-empty strings")
        return value


class ChatCompletionMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None
    tool_calls: list[dict[str, Any]] | None = None
    refusal: str | None = None


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatCompletionMessage
    finish_reason: Literal["stop", "length", "content_filter", "tool_calls"]


class CompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletion(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: CompletionUsage | None
    system_fingerprint: str | None = None
    gateway_warnings: list[str] = Field(default_factory=list)


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]
