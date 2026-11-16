from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.shared.dtos.model_features import ModelFeatureMetadata


class ProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    kind: Literal["openai", "google", "openrouter", "anthropic", "offline", "local"]
    purpose: Literal["target"] = "target"
    status: Literal["active", "inactive"] = "active"
    base_url: str | None = None


class ProviderRead(ProviderCreate):
    id: str
    created_at: datetime
    updated_at: datetime


class ModelCreate(BaseModel):
    provider_id: str
    name: str = Field(min_length=1, max_length=150)
    display_name: str = Field(min_length=1, max_length=200)
    status: Literal["active", "inactive"] = "inactive"
    capabilities: list[str] = Field(default_factory=list)
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    feature_metadata: ModelFeatureMetadata | None = None
    target_latency_ms: int | None = Field(default=None, ge=0)
    input_price_per_million: float = Field(default=0.0, ge=0)
    output_price_per_million: float = Field(default=0.0, ge=0)
    pricing_version: str = "1"
    quality_tier: str = Field(default="standard", min_length=1, max_length=30)


class ModelRead(ModelCreate):
    id: str
    created_at: datetime
    updated_at: datetime


class ModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "inactive"] | None = None
    capabilities: list[str] | None = None
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    feature_metadata: ModelFeatureMetadata | None = None
    target_latency_ms: int | None = Field(default=None, ge=0)
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)
    pricing_version: str | None = Field(default=None, min_length=1, max_length=50)
    quality_tier: str | None = Field(default=None, min_length=1, max_length=30)

    @model_validator(mode="after")
    def require_update(self):
        if not self.model_fields_set:
            raise ValueError("At least one supported field must be provided")
        if any(getattr(self, field) is None for field in self.model_fields_set - {"status"}):
            raise ValueError("Updated model metadata fields must not be null")
        return self


class ModelPoolMemberCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    # One primary per tier (simple / mid / hard); fallbacks are tried before another tier.
    role: Literal["primary", "fallback"] = "primary"


class ModelPoolMemberRead(ModelPoolMemberCreate):
    id: str


class ModelPoolCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    purpose: str = Field(min_length=1, max_length=100)
    status: Literal["active", "inactive"] = "active"
    members: list[ModelPoolMemberCreate] = Field(min_length=1)


class ModelPoolUpdate(ModelPoolCreate):
    """Full replacement of a user-owned model set."""


class ModelPoolRead(ModelPoolCreate):
    id: str
    owner_user_id: str | None
    members: list[ModelPoolMemberRead]
    # Non-blocking notes: a pool made before the 3-tier rule, a price ladder that
    # runs backwards, models from several providers, models that cannot be called.
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class FallbackSuggestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_ids: list[str] = Field(min_length=1, max_length=10)
    # Consent to fall back to another provider's model when the primary fails.
    allow_other_providers: bool = True


class FallbackSuggestion(BaseModel):
    for_model_id: str
    tier: str
    model_id: str | None
    reason: str
    # Price change vs the primary, as output price per million tokens (USD).
    output_price_delta_per_million: float | None = None


class PolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    match_conditions: dict = Field(default_factory=dict)
    guardrails: dict = Field(default_factory=dict)
    fallback_chain: list[str] = Field(default_factory=list)
    priority: int = Field(default=100, ge=1)
    status: Literal["active", "inactive"] = "active"


class PolicyRead(PolicyCreate):
    id: str
    created_at: datetime
    updated_at: datetime
