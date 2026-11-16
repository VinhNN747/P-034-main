"""A customer's own OpenAI-compatible model server (bring your own endpoint) and its models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

QualityTier = Literal["simple", "mid", "hard"]
# Capabilities a customer may declare on top of plain text generation (routing filters on them).
ExtraCapability = Literal["code_generation", "reasoning", "long_context", "structured_output"]


def _trimmed(value: str | None) -> str | None:
    if value is None:
        return value
    value = value.strip()
    if not value:
        raise ValueError("Value must not be blank")
    return value


class EndpointCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    """The server's OpenAI-compatible root, e.g. https://gpu.example.com/v1 (vLLM, Ollama behind a proxy)."""
    api_key: SecretStr | None = None
    """Sent as the bearer token; optional for servers without authentication."""

    @field_validator("display_name", "base_url")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return _trimmed(value)

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value().strip()) < 5:
            raise ValueError("API key must contain at least 5 characters")
        return value


class EndpointModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=150)
    """Exactly the model name the server serves (vLLM --served-model-name, an Ollama tag)."""
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    quality_tier: QualityTier
    context_window: int | None = Field(default=None, ge=256)
    capabilities: list[ExtraCapability] = Field(default_factory=list)
    """Declared on top of chat/text, e.g. structured_output when the server enforces JSON."""

    @field_validator("name", "display_name")
    @classmethod
    def trim_text(cls, value: str | None) -> str | None:
        return _trimmed(value)


class EndpointModelRead(BaseModel):
    id: str
    name: str
    display_name: str
    quality_tier: str
    context_window: int | None
    capabilities: list[str]
    status: Literal["active", "inactive"]
    health_status: str
    last_probe_at: datetime | None
    last_probe_latency_ms: float | None
    last_probe_error: str | None


class EndpointRead(BaseModel):
    id: str
    display_name: str
    base_url: str
    status: Literal["active", "inactive"]
    has_api_key: bool
    key_last4: str | None
    models: list[EndpointModelRead]
    created_at: datetime
    updated_at: datetime
