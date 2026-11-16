from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

FeatureSupport = Literal["supported", "conditional", "unverified", "unsupported"]


class ModelFeatureDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    support: FeatureSupport
    conditions: list[str] = Field(default_factory=list)
    source_url: HttpUrl


class ModelFeatureConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thinking_mode: Literal["adaptive", "always_on", "adaptive_legacy"]
    default_effort: Literal["low", "medium", "high"]
    forced_tool_choice: FeatureSupport
    custom_sampling: bool
    prompt_caching: bool
    token_counting: bool


class ModelPricingMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: Literal["USD"] = "USD"
    standard_input_per_million: float = Field(ge=0)
    standard_output_per_million: float = Field(ge=0)
    long_context_input_threshold: int | None = Field(default=None, ge=1)
    long_context_threshold_operator: Literal[">", ">="] = ">"
    long_context_input_per_million: float | None = Field(default=None, ge=0)
    long_context_output_per_million: float | None = Field(default=None, ge=0)
    cache_write_5m_input_per_million: float = Field(ge=0)
    cache_write_1h_input_per_million: float = Field(ge=0)
    cache_read_input_per_million: float = Field(ge=0)
    batch_discount_fraction: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def validate_long_context_prices(self):
        long_context_prices = (self.long_context_input_per_million, self.long_context_output_per_million)
        if self.long_context_input_threshold is None and any(price is not None for price in long_context_prices):
            raise ValueError("Long-context prices require a token threshold")
        if self.long_context_input_threshold is not None and any(price is None for price in long_context_prices):
            raise ValueError("Long-context input and output prices must be provided together")
        return self


class ModelLifecycleMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: Literal["active", "legacy", "deprecated"]
    reliable_knowledge_cutoff: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    retirement_commitment: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ModelProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checked_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    verification: Literal["documentation_only", "live_verified"]
    sources: list[HttpUrl] = Field(min_length=1)


class ModelLatencyReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["openrouter"]
    provider: Literal["Anthropic"]
    service_tier: Literal["standard"]
    source_metric: Literal["latency_p50"]
    metric_interpretation: Literal["ttft"]
    latency_ms: float = Field(ge=0)
    throughput_tokens_per_second: float = Field(ge=0)
    percentile: Literal[50]
    reasoning_effort: Literal["all"]
    locations: Literal["all"]
    window: Literal["1 week"]
    retrieved_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    source_url: HttpUrl
    verification: Literal["external_reference"]


class ModelFeatureMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    upstream_features: dict[str, ModelFeatureDetail]
    gateway_features: dict[str, ModelFeatureDetail]
    constraints: ModelFeatureConstraints
    pricing: ModelPricingMetadata
    lifecycle: ModelLifecycleMetadata
    provenance: ModelProvenance
    latency_reference: ModelLatencyReference | None = None
