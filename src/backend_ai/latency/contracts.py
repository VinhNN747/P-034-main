from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Convert immutable contract values into JSON-compatible containers."""
    if isinstance(value, Mapping):
        return {str(key): thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [thaw(item) for item in value]
    return value


def _validate_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ModelKey:
    catalog_model_id: str
    provider_id: str
    upstream_model_name: str
    connection_fingerprint: str
    provider_kind: str = "unknown"

    def __post_init__(self) -> None:
        for name in ("catalog_model_id", "provider_id", "upstream_model_name", "connection_fingerprint"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")

    def digest(self) -> str:
        identity = {
            "catalog_model_id": self.catalog_model_id,
            "provider_id": self.provider_id,
            "provider_kind": self.provider_kind,
            "upstream_model_name": self.upstream_model_name,
            "connection_fingerprint": self.connection_fingerprint,
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def connection_fingerprint(
    kind: str,
    endpoint: str | None,
    credential_version: int | None,
    region: str,
) -> str:
    defaults = {
        "openai": "https://api.openai.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "google": "https://generativelanguage.googleapis.com/v1beta",
        "anthropic": "https://api.anthropic.com",
    }
    selected = (endpoint or defaults.get(kind.lower()) or f"{kind.lower()}://default").strip()
    parts = urlsplit(selected)
    # Strip user-info, query and fragment because they can contain credentials. A
    # connection's secret rotation is represented by credential_version instead.
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port is not None else ""
    netloc = f"{host}{port}" if host else ""
    path = parts.path.rstrip("/")
    canonical = urlunsplit((parts.scheme.lower(), netloc, path, "", ""))
    payload = {
        "kind": kind.lower(),
        "endpoint": canonical,
        "credential_version": credential_version,
        "region": region.strip().lower(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class LatencyConfig:
    mode: Literal["off", "auto"] = "auto"
    training_enabled: bool = True
    history_refresh_seconds: int = 30
    history_max_age_seconds: int = 60
    observation_queue_size: int = 4096
    writer_batch_size: int = 100
    writer_flush_seconds: float = 1.0
    writer_retries: int = 3
    training_poll_seconds: int = 60
    training_interval_seconds: int = 3600
    min_training_samples: int = 500
    min_new_samples: int = 200
    min_profile_train_samples: int = 50
    min_profile_validation_samples: int = 20
    min_common_validation_samples: int = 100
    max_observation_age_seconds: int = 1800
    training_window_days: int = 7
    retention_days: int = 30
    max_training_rows: int = 20_000
    max_features: int = 256
    max_model_groups: int = 128
    max_artifact_bytes: int = 2 * 1024 * 1024
    training_timeout_seconds: int = 300
    training_max_rss_mb: int = 512
    training_max_api_inflight: int = 1
    work_dir: str = "data/latency"
    region: str = "unspecified"
    ridge_alpha: float = 1.0
    residual_half_life_seconds: float = 300.0
    residual_clip_log: float = math.log(2)
    min_residual_samples: int = 20
    min_profile_recent_successes: int = 50
    min_profile_recent_pairs: int = 20
    max_timeout_fraction: float = 0.05
    max_censored_fraction: float = 0.10
    max_wape: float = 0.35
    max_candidate_catalog_mae_ratio: float = 1.05
    max_publish_catalog_mae_ratio: float = 0.95
    max_latency_ms: float = 600_000.0
    stream_pause_exclusion_ms: float = 100.0
    training_feature_decay_seconds: float = 86_400.0
    max_event_bytes: int = 8192
    queue_drop_retries: int = 3

    @classmethod
    def from_settings(cls, settings: Any) -> LatencyConfig:
        values = {
            "mode": settings.gateway_latency_mode,
            "training_enabled": settings.gateway_latency_training_enabled,
            "history_refresh_seconds": settings.gateway_latency_history_refresh_seconds,
            "history_max_age_seconds": settings.gateway_latency_history_max_age_seconds,
            "observation_queue_size": settings.gateway_latency_observation_queue_size,
            "writer_batch_size": settings.gateway_latency_writer_batch_size,
            "writer_flush_seconds": settings.gateway_latency_writer_flush_seconds,
            "writer_retries": settings.gateway_latency_writer_retries,
            "training_poll_seconds": settings.gateway_latency_training_poll_seconds,
            "training_interval_seconds": settings.gateway_latency_training_interval_seconds,
            "min_training_samples": settings.gateway_latency_min_training_samples,
            "min_new_samples": settings.gateway_latency_min_new_samples,
            "min_profile_train_samples": settings.gateway_latency_min_profile_train_samples,
            "min_profile_validation_samples": settings.gateway_latency_min_profile_validation_samples,
            "min_common_validation_samples": settings.gateway_latency_min_common_validation_samples,
            "max_observation_age_seconds": settings.gateway_latency_max_observation_age_seconds,
            "training_window_days": settings.gateway_latency_training_window_days,
            "retention_days": settings.gateway_latency_retention_days,
            "max_training_rows": settings.gateway_latency_max_training_rows,
            "max_features": settings.gateway_latency_max_features,
            "max_model_groups": settings.gateway_latency_max_model_groups,
            "max_artifact_bytes": settings.gateway_latency_max_artifact_bytes,
            "training_timeout_seconds": settings.gateway_latency_training_timeout_seconds,
            "training_max_rss_mb": settings.gateway_latency_training_max_rss_mb,
            "training_max_api_inflight": settings.gateway_latency_training_max_api_inflight,
            "work_dir": settings.gateway_latency_work_dir,
            "region": settings.gateway_latency_region,
        }
        return cls(**values)


@dataclass(frozen=True, slots=True)
class LatencyFeatures:
    model_key: ModelKey
    profile_key: str
    observed_at: datetime
    values: Mapping[str, float | int | str | bool | None]
    schema_version: str = "latency-v1"

    def __post_init__(self) -> None:
        _validate_utc(self.observed_at, "observed_at")
        object.__setattr__(self, "values", _freeze(self.values))


@dataclass(frozen=True, slots=True)
class LatencyObservation:
    attempt_id: str
    request_id: str
    task_id: str | None
    application_id: str | None
    features: LatencyFeatures
    source: str
    mode: str
    dispatched_at: datetime
    status: Literal["success", "error", "timeout", "cancelled", "incomplete"]
    finished_at: datetime | None = None
    provider_total_ms: float | None = None
    ttft_ms: float | None = None
    consumer_pause_ms: float = 0.0
    catalog_ms: float | None = None
    predicted_ms: float | None = None
    prediction_version: str | None = None
    error_category: str | None = None
    execution_mode: str = "live"
    training_eligible: bool = True
    actual_payload_chars: int | None = None
    resolution_source: str | None = None
    resolution_reason: str | None = None

    def __post_init__(self) -> None:
        _validate_utc(self.dispatched_at, "dispatched_at")
        if self.finished_at is not None:
            _validate_utc(self.finished_at, "finished_at")
        for name in ("provider_total_ms", "ttft_ms", "consumer_pause_ms", "catalog_ms", "predicted_ms"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.provider_total_ms is not None and self.provider_total_ms <= 0:
            raise ValueError("provider_total_ms must be positive")
        if self.consumer_pause_ms < 0:
            raise ValueError("consumer_pause_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class ProfileStats:
    train_count: int = 0
    validation_count: int = 0
    recent_success_count: int = 0
    recent_count: int = 0
    newest_finished_at: datetime | None = None
    timeout_rate_30m: float | None = None
    censored_rate_30m: float | None = None
    error_rate_30m: float | None = None
    inter_arrival_ms: float | None = None
    measurement_age_ms: float | None = None
    ewma_ms: float | None = None
    median_ms: float | None = None
    p90_ms: float | None = None
    std_ms: float | None = None
    success_count_30m: int = 0
    local_inflight: float = 0.0
    local_rpm: float = 0.0
    input_proxy_min: float | None = None
    input_proxy_max: float | None = None
    output_budget_min: float | None = None
    output_budget_max: float | None = None
    candidate_wape: float | None = None
    candidate_mae_ms: float | None = None
    catalog_mae_ms: float | None = None
    ewma_mae_ms: float | None = None
    recent_pairs: tuple[Mapping[str, Any], ...] = ()
    residuals: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.newest_finished_at is not None:
            _validate_utc(self.newest_finished_at, "newest_finished_at")
        object.__setattr__(self, "recent_pairs", tuple(_freeze(item) for item in self.recent_pairs))
        object.__setattr__(self, "residuals", tuple(_freeze(item) for item in self.residuals))


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    built_at: datetime
    profiles: Mapping[str, ProfileStats]

    def __post_init__(self) -> None:
        _validate_utc(self.built_at, "built_at")
        object.__setattr__(self, "profiles", _freeze(self.profiles))


@dataclass(frozen=True, slots=True)
class LatencyArtifact:
    schema_version: str
    version: str
    feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float
    numeric_means: Mapping[str, float] = field(default_factory=dict)
    numeric_scales: Mapping[str, float] = field(default_factory=dict)
    categories: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    support: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    validation_report: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = ""

    def __post_init__(self) -> None:
        if len(self.feature_names) != len(self.coefficients):
            raise ValueError("feature_names and coefficients must have the same length")
        if not math.isfinite(self.intercept) or not all(math.isfinite(value) for value in self.coefficients):
            raise ValueError("artifact parameters must be finite")
        object.__setattr__(self, "numeric_means", _freeze(self.numeric_means))
        object.__setattr__(self, "numeric_scales", _freeze(self.numeric_scales))
        object.__setattr__(self, "categories", _freeze(self.categories))
        object.__setattr__(self, "support", _freeze(self.support))
        object.__setattr__(self, "validation_report", _freeze(self.validation_report))


@dataclass(frozen=True, slots=True)
class LatencyResolution:
    effective_ms: float | None
    predicted_ms: float | None
    source: Literal["catalog", "predicted", "unknown"]
    gate_reason: str
    model_version: str | None
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.effective_ms is not None and (not math.isfinite(self.effective_ms) or self.effective_ms <= 0):
            raise ValueError("effective_ms must be finite and positive when present")
        if self.predicted_ms is not None and (not math.isfinite(self.predicted_ms) or self.predicted_ms <= 0):
            raise ValueError("predicted_ms must be finite and positive when present")
        object.__setattr__(self, "evidence", _freeze(self.evidence))
