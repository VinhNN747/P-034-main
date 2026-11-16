from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator


class ResponseInputTextPart(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    type: Literal["input_text"]
    text: StrictStr


class ResponseOutputTextPart(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    type: Literal["output_text"]
    text: StrictStr
    annotations: list[Any] = Field(default_factory=list)
    logprobs: list[Any] = Field(default_factory=list)

    @model_validator(mode="after")
    def no_output_metadata(self):
        if self.annotations:
            raise ValueError("assistant history with annotations is unsupported")
        if self.logprobs:
            raise ValueError("assistant history with logprobs is unsupported")
        return self


ResponseContentPart = Annotated[ResponseInputTextPart | ResponseOutputTextPart, Field(discriminator="type")]


class ResponseInputMessage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    role: Literal["system", "developer", "user", "assistant"]
    content: StrictStr | list[ResponseContentPart]
    type: Literal["message"] | None = None
    id: StrictStr | None = None
    status: Literal["in_progress", "completed", "incomplete"] | None = None
    phase: Literal["final_answer"] | None = None

    @field_validator("type", "id", "status", "phase", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("null is not supported for this field")
        return value

    @model_validator(mode="after")
    def validate_role_shape(self):
        if not self.content:
            raise ValueError("message content must not be empty")
        if self.role == "assistant":
            if self.type != "message" or not self.id or self.status not in {"completed", "incomplete"}:
                raise ValueError("assistant history must be a completed or incomplete output message")
            if not isinstance(self.content, list) or any(
                not isinstance(part, ResponseOutputTextPart) for part in self.content
            ):
                raise ValueError("assistant history may contain only output_text parts")
        else:
            if self.id is not None or self.status is not None or self.phase is not None:
                raise ValueError("input messages cannot contain assistant output fields")
            if isinstance(self.content, list) and any(
                not isinstance(part, ResponseInputTextPart) for part in self.content
            ):
                raise ValueError("input messages may contain only input_text parts")
        return self


class ResponseFunctionCall(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    type: Literal["function_call"]
    id: StrictStr | None = None
    call_id: StrictStr = Field(min_length=1)
    name: StrictStr = Field(min_length=1)
    arguments: StrictStr
    status: Literal["in_progress", "completed", "incomplete"] | None = None


class ResponseFunctionCallOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    type: Literal["function_call_output"]
    call_id: StrictStr = Field(min_length=1)
    output: StrictStr
    id: StrictStr | None = None
    status: Literal["in_progress", "completed", "incomplete"] | None = None


ResponseInputItem = ResponseInputMessage | ResponseFunctionCall | ResponseFunctionCallOutput


class ResponseTextFormat(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ResponseTextFormatText(ResponseTextFormat):
    type: Literal["text"]


class ResponseTextFormatJSONObject(ResponseTextFormat):
    type: Literal["json_object"]


class ResponseTextFormatJSONSchema(ResponseTextFormat):
    model_config = ConfigDict(strict=True, extra="forbid", populate_by_name=True)
    type: Literal["json_schema"]
    name: StrictStr = Field(min_length=1, max_length=64)
    schema_: dict[str, Any] = Field(alias="schema")
    description: StrictStr | None = None
    strict: StrictBool | None = None

    @field_validator("description", "strict", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("null is not supported for this field")
        return value


ResponseTextFormatUnion = Annotated[
    ResponseTextFormatText | ResponseTextFormatJSONObject | ResponseTextFormatJSONSchema,
    Field(discriminator="type"),
]


class ResponseTextConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    format: ResponseTextFormatUnion | None = None
    verbosity: Literal["low", "medium", "high"] | None = None

    @field_validator("format", "verbosity", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("null is not supported for this field")
        return value


class ResponseStreamOptions(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    include_obfuscation: StrictBool | None = None

    @field_validator("include_obfuscation", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("null is not supported for this field")
        return value


class ResponseConversation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: StrictStr = Field(min_length=1)


class ResponsePrompt(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: StrictStr = Field(min_length=1)
    variables: dict[StrictStr, Any] | None = None
    version: StrictStr | None = None


class ResponseReasoning(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    context: Literal["auto", "current_turn", "all_turns"] | None = None
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    generate_summary: Literal["auto", "concise", "detailed"] | None = None
    mode: StrictStr | None = None
    summary: Literal["auto", "concise", "detailed"] | None = None


class ResponsePromptCacheOptions(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    mode: Literal["implicit", "explicit"] | None = None
    ttl: Literal["30m"] | None = None


class ResponseCreateRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    model: StrictStr = Field(min_length=1)
    input: Annotated[StrictStr, Field(min_length=1)] | Annotated[
        list[ResponseInputItem], Field(min_length=1, max_length=200)]
    instructions: StrictStr | None = None
    store: StrictBool | None = True
    stream: StrictBool = False
    text: ResponseTextConfig = Field(default_factory=ResponseTextConfig)
    metadata: dict[StrictStr, StrictStr] | None = None
    background: StrictBool | None = None
    context_management: list[dict[str, Any]] | None = None
    conversation: StrictStr | ResponseConversation | None = None
    include: list[StrictStr] | None = None
    max_output_tokens: StrictInt | None = Field(default=None, gt=0, le=32_768)
    max_tool_calls: StrictInt | None = Field(default=None, gt=0)
    moderation: dict[str, Any] | None = None
    parallel_tool_calls: StrictBool | None = None
    previous_response_id: StrictStr | None = None
    prompt: ResponsePrompt | None = None
    prompt_cache_key: StrictStr | None = None
    prompt_cache_options: ResponsePromptCacheOptions | None = None
    prompt_cache_retention: Literal["in_memory", "24h"] | None = None
    reasoning: ResponseReasoning | None = None
    safety_identifier: StrictStr | None = None
    service_tier: Literal["auto", "default", "flex", "scale", "priority", "fast"] | None = None
    stream_options: ResponseStreamOptions | None = None
    temperature: float | None = None
    tool_choice: Literal["none", "auto", "required"] | dict[str, Any] | None = None
    tools: list[Any] | None = None
    top_logprobs: StrictInt | None = Field(default=None, ge=0, le=20)
    top_p: float | None = None
    truncation: Literal["auto", "disabled"] | None = None
    user: StrictStr | None = None

    @field_validator(
        "instructions", "store", "background", "context_management", "conversation", "include",
        "max_output_tokens", "max_tool_calls", "metadata", "moderation", "parallel_tool_calls",
        "previous_response_id", "prompt", "prompt_cache_key", "prompt_cache_options",
        "prompt_cache_retention", "reasoning", "safety_identifier", "service_tier",
        "stream_options", "tool_choice", "tools", "top_logprobs", "top_p", "truncation", "user",
        mode="before",
    )
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("null is not supported for this parameter")
        return value

    @field_validator("input")
    @classmethod
    def input_not_empty(cls, value):
        if isinstance(value, str) and not value:
            raise ValueError("input must not be empty")
        if isinstance(value, list) and not value:
            raise ValueError("input must not be empty")
        return value

    @field_validator("temperature", "top_p", mode="before")
    @classmethod
    def finite_sampling_values(cls, value):
        if value is None:
            raise ValueError("null is not supported for this parameter")
        if type(value) not in (int, float):
            raise ValueError("value must be a number")
        import math
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        return value

    @field_validator("metadata")
    @classmethod
    def metadata_bounds(cls, value):
        if value is not None:
            if len(value) > 16:
                raise ValueError("metadata supports at most 16 key-value pairs")
            if any(len(key) > 64 or len(item) > 512 for key, item in value.items()):
                raise ValueError("metadata key/value exceeds the supported length")
        return value

    @model_validator(mode="after")
    def sampling_ranges(self):
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if self.top_p is not None and not 0 <= self.top_p <= 1:
            raise ValueError("top_p must be between 0 and 1")
        return self


class ResponseIncompleteDetails(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    reason: Literal["max_output_tokens", "content_filter"] | None = None


class ResponseUsageInputDetails(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    cached_tokens: StrictInt = Field(ge=0)
    cache_write_tokens: StrictInt = Field(ge=0)


class ResponseUsageOutputDetails(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    reasoning_tokens: StrictInt = Field(ge=0)


class ResponseUsage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    input_tokens: StrictInt = Field(ge=0)
    input_tokens_details: ResponseUsageInputDetails
    output_tokens: StrictInt = Field(ge=0)
    output_tokens_details: ResponseUsageOutputDetails
    total_tokens: StrictInt = Field(ge=0)


class ResponseRefusalPart(BaseModel):
    type: Literal["refusal"] = "refusal"
    refusal: StrictStr


class ResponseOutputMessage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: StrictStr
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    status: Literal["completed", "incomplete"]
    phase: Literal["final_answer"] = "final_answer"
    content: list[ResponseOutputTextPart | ResponseRefusalPart]


class ResponseMeasuredUsage(BaseModel):
    """Measured final-producer totals; unknown detail counters stay null.

    Kept in an extension because the pinned SDK requires integer counters
    in its standard usage object. Never invent those counters to pass it.
    """
    input_tokens: StrictInt = Field(ge=0)
    output_tokens: StrictInt = Field(ge=0)
    total_tokens: StrictInt = Field(ge=0)
    cached_tokens: StrictInt | None = None
    cache_write_tokens: StrictInt | None = None
    reasoning_tokens: StrictInt | None = None


class ResponseObject(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: StrictStr
    created_at: float
    error: dict[str, Any] | None = None
    incomplete_details: ResponseIncompleteDetails | None = None
    instructions: StrictStr | None = None
    metadata: dict[StrictStr, StrictStr] | None = None
    model: StrictStr
    object: Literal["response"] = "response"
    output: list[ResponseOutputMessage | ResponseFunctionCall]
    parallel_tool_calls: StrictBool = False
    temperature: float | None = None
    tool_choice: Literal["none", "auto", "required"] | dict[str, Any] = "none"
    tools: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["completed", "incomplete"]
    text: ResponseTextConfig = Field(default_factory=ResponseTextConfig)
    top_p: float | None = None
    usage: ResponseUsage | None = None
    store: StrictBool = True
    previous_response_id: StrictStr | None = None
    max_output_tokens: StrictInt | None = None
    gateway_warnings: list[StrictStr] = Field(default_factory=list)
    gateway_usage: ResponseMeasuredUsage | None = None

    @field_validator("error", "instructions", "metadata", "temperature", "top_p", "incomplete_details", "usage", mode="before")
    @classmethod
    def allow_none_response_fields(cls, value):
        return value
