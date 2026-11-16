from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ProbeMode = Literal["chat", "embedding", "ocr", "jev"]
ProbeStatus = Literal["healthy", "failed", "unavailable", "skipped"]


class ConnectionProbeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    mode: ProbeMode
    model_name: str | None = Field(default=None, min_length=1, max_length=150)

    @field_validator("model_name")
    @classmethod
    def trim_model_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Model name must not be blank")
        return normalized


class ConnectionProbeRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    credential_version: int | None
    status: ProbeStatus
    mode: ProbeMode
    model_name: str | None
    checked_at: datetime
    latency_ms: float = Field(ge=0)
    error_code: str | None = None
    error_message: str | None = None
