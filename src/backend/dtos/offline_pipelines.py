"""Offline pipelines: a user's own self-hosted models behind their own API key, routed by the task classifier.

Stored as an application with ``pipeline = "offline"`` (keys, usage, request log and billing are shared with
applications), but created, listed and edited only through /gateway/offline-pipelines.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OfflineTier = Literal["simple", "mid", "hard"]


class OfflineLadder(BaseModel):
    """One model id per tier, each served by one of the user's self-hosted endpoints."""

    model_config = ConfigDict(extra="forbid")

    simple: str = Field(min_length=1, max_length=36)
    mid: str = Field(min_length=1, max_length=36)
    hard: str = Field(min_length=1, max_length=36)


def _check_thresholds(mid: float | None, large: float | None) -> None:
    if (mid is None) != (large is None):
        raise ValueError("Set both classifier thresholds, or neither to use the server defaults.")
    if mid is not None and large is not None and not mid < large:
        raise ValueError("The Mid threshold must be lower than the Large threshold.")


class OfflinePipelineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    models: OfflineLadder
    # None = GATEWAY_CLASSIFIER_MID_DIFFICULTY / _LARGE_DIFFICULTY of the server.
    classifier_mid_difficulty: float | None = Field(default=None, ge=0, le=1)
    classifier_large_difficulty: float | None = Field(default=None, ge=0, le=1)
    max_output_tokens: int = Field(default=800, ge=1, le=32_768)
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    fallback_enabled: bool = True

    @model_validator(mode="after")
    def _thresholds(self):
        _check_thresholds(self.classifier_mid_difficulty, self.classifier_large_difficulty)
        return self


class OfflinePipelineUpdate(BaseModel):
    """Only the fields sent are changed. Sending both thresholds as null returns to the server defaults."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "paused"] | None = None
    models: OfflineLadder | None = None
    classifier_mid_difficulty: float | None = Field(default=None, ge=0, le=1)
    classifier_large_difficulty: float | None = Field(default=None, ge=0, le=1)
    max_output_tokens: int | None = Field(default=None, ge=1, le=32_768)
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    fallback_enabled: bool | None = None

    @model_validator(mode="after")
    def _thresholds(self):
        sent = {"classifier_mid_difficulty", "classifier_large_difficulty"} & self.model_fields_set
        if sent:
            if len(sent) == 1:
                raise ValueError("Change both classifier thresholds together.")
            _check_thresholds(self.classifier_mid_difficulty, self.classifier_large_difficulty)
        return self


class OfflineModelRead(BaseModel):
    tier: OfflineTier
    model_id: str
    name: str
    display_name: str
    endpoint_id: str
    endpoint_name: str
    status: str
    health_status: str
    context_window: int | None


class OfflinePipelineRead(BaseModel):
    id: str
    slug: str
    name: str
    description: str | None
    status: Literal["active", "paused", "archived"]
    models: list[OfflineModelRead]
    classifier_mid_difficulty: float
    classifier_large_difficulty: float
    """Effective thresholds: the pipeline's own, or the server defaults when ``thresholds_default``."""
    thresholds_default: bool
    max_output_tokens: int
    timeout_seconds: float
    fallback_enabled: bool
    request_count: int = 0
    config_version: int | None = None
    created_at: datetime
    updated_at: datetime
