from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime

from src.backend_ai.latency.contracts import (
    HistorySnapshot,
    LatencyArtifact,
    LatencyConfig,
    LatencyFeatures,
    LatencyResolution,
    ProfileStats,
)

_CATEGORICAL_FEATURES = ("model_key_hash", "provider_kind", "protocol", "task_type")
_NUMERIC_FEATURES = (
    "streaming", "has_tools", "has_schema", "n", "log_input_proxy", "log_output_budget",
    "output_budget_missing", "reasoning", "reasoning_missing", "risk", "risk_missing",
    "hour_sin", "hour_cos", "weekday_sin", "weekday_cos", "log_inter_arrival_ms",
    "inter_arrival_missing", "log_measurement_age_ms", "history_missing", "log_ewma_ms",
    "log_median_ms", "log_p90_ms", "log_std_ms", "error_rate_30m", "timeout_rate_30m",
    "log_success_count_30m", "log_local_inflight", "log_local_rpm",
)
_LOG_FEATURES = {
    "log_input_proxy": "input_proxy",
    "log_output_budget": "output_budget",
    "log_inter_arrival_ms": "inter_arrival_ms",
    "log_measurement_age_ms": "measurement_age_ms",
    "log_ewma_ms": "ewma_ms",
    "log_median_ms": "median_ms",
    "log_p90_ms": "p90_ms",
    "log_std_ms": "std_ms",
    "log_success_count_30m": "success_count_30m",
    "log_local_inflight": "local_inflight",
    "log_local_rpm": "local_rpm",
}


def _valid_positive(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0


def _catalog_resolution(
    catalog_ms: float | None,
    reason: str,
    version: str | None,
    *,
    predicted_ms: float | None = None,
    evidence: Mapping[str, object] | None = None,
) -> LatencyResolution:
    if _valid_positive(catalog_ms):
        return LatencyResolution(float(catalog_ms), predicted_ms, "catalog", reason, version, evidence or {})
    return LatencyResolution(None, predicted_ms, "unknown", reason, version, evidence or {})


def _category_value(features: LatencyFeatures, name: str) -> str:
    value = features.values.get(name)
    return str(value) if value is not None and str(value) else "unknown"


def _numeric_value(features: LatencyFeatures, feature_name: str) -> float:
    source_name = _LOG_FEATURES.get(feature_name, feature_name)
    raw = features.values.get(source_name)
    if raw is None:
        return 0.0
    if not isinstance(raw, (bool, int, float)) or not math.isfinite(float(raw)):
        raise ValueError(f"invalid numeric feature: {source_name}")
    number = float(raw)
    if feature_name in _LOG_FEATURES:
        if number < 0:
            raise ValueError(f"negative log feature: {source_name}")
        return math.log1p(number)
    return number


def _predict_log(artifact: LatencyArtifact, features: LatencyFeatures) -> float:
    encoded: list[float] = []
    for feature_name in artifact.feature_names:
        if feature_name.startswith("cat:"):
            category, expected = feature_name[4:].split("=", 1)
            encoded.append(float(_category_value(features, category) == expected))
        elif feature_name.startswith("num:"):
            name = feature_name[4:]
            if name not in _NUMERIC_FEATURES:
                raise ValueError("artifact has an unknown numeric feature")
            scale = artifact.numeric_scales.get(name)
            mean = artifact.numeric_means.get(name)
            if scale is None or mean is None or not math.isfinite(scale) or scale <= 0 or not math.isfinite(mean):
                raise ValueError("artifact scaler is incomplete")
            encoded.append((_numeric_value(features, name) - mean) / scale)
        else:
            raise ValueError("artifact feature name has an unsupported prefix")
    if len(encoded) != len(artifact.coefficients):
        raise ValueError("artifact coefficient dimension mismatch")
    return artifact.intercept + sum(
        coefficient * value for coefficient, value in zip(artifact.coefficients, encoded, strict=True)
    )


def _coverage_ok(support: Mapping[str, object], features: LatencyFeatures) -> bool:
    input_proxy = features.values.get("input_proxy")
    input_low = support.get("input_proxy_p05")
    input_high = support.get("input_proxy_p95")
    if not _valid_finite(input_proxy) or not _valid_finite(input_low) or not _valid_finite(input_high):
        return False
    if not _inside(float(input_proxy), float(input_low), float(input_high)):
        return False

    budget = features.values.get("output_budget")
    if budget is None:
        return int(support.get("train_missing_budget", 0)) > 0 and int(support.get("validation_missing_budget", 0)) > 0
    budget_low = support.get("output_budget_p05")
    budget_high = support.get("output_budget_p95")
    return (
        _valid_finite(budget)
        and _valid_finite(budget_low)
        and _valid_finite(budget_high)
        and _inside(float(budget), float(budget_low), float(budget_high))
    )


def _valid_finite(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _inside(value: float, low: float, high: float) -> bool:
    if high < low:
        return False
    span = high - low
    tolerance = 0.25 * span if span > 0 else max(1.0, 0.25 * abs(value))
    return low - tolerance <= value <= high + tolerance


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None and value.utcoffset() is not None else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    return None


def _recent_metrics_ok(stats: ProfileStats, now: datetime, config: LatencyConfig) -> bool:
    common = []
    for pair in stats.recent_pairs:
        finished_at = _timestamp(pair.get("finished_at"))
        if finished_at is None or finished_at > now:
            continue
        values = (pair.get("predicted_ms"), pair.get("actual_ms"), pair.get("catalog_ms"), pair.get("ewma_ms"))
        if all(_valid_positive(value) for value in values):
            common.append(tuple(float(value) for value in values))
    if len(common) < config.min_profile_recent_pairs:
        return True
    predicted = [row[0] for row in common]
    actual = [row[1] for row in common]
    catalog = [row[2] for row in common]
    ewma = [row[3] for row in common]
    candidate_errors = [abs(value - target) for value, target in zip(predicted, actual, strict=True)]
    candidate_mae = sum(candidate_errors) / len(common)
    actual_total = sum(actual)
    candidate_wape = sum(candidate_errors) / actual_total if actual_total > 0 else math.inf
    catalog_mae = sum(abs(value - target) for value, target in zip(catalog, actual, strict=True)) / len(common)
    ewma_mae = sum(abs(value - target) for value, target in zip(ewma, actual, strict=True)) / len(common)
    return (
        candidate_wape <= config.max_wape
        and candidate_mae <= config.max_candidate_catalog_mae_ratio * catalog_mae
        and candidate_mae <= ewma_mae
    )


def _residual_correction(stats: ProfileStats, now: datetime, config: LatencyConfig) -> tuple[float, int]:
    accepted = []
    for residual in stats.residuals:
        finished_at = _timestamp(residual.get("finished_at"))
        value = residual.get("residual_log")
        if finished_at is not None and finished_at <= now and _valid_finite(value):
            accepted.append((finished_at, float(value)))
    if len(accepted) < config.min_residual_samples:
        return 0.0, len(accepted)
    total_weight = weighted = 0.0
    for finished_at, value in accepted:
        age = max(0.0, (now - finished_at).total_seconds())
        weight = math.exp(-math.log(2) * age / config.residual_half_life_seconds)
        weighted += value * weight
        total_weight += weight
    correction = weighted / total_weight if total_weight else 0.0
    return max(-config.residual_clip_log, min(config.residual_clip_log, correction)), len(accepted)


class LatencyResolver:
    """Pure request-path predictor over immutable snapshots; never reads DB or disk."""

    def __init__(
        self,
        artifact: LatencyArtifact | None,
        history: HistorySnapshot,
        config: LatencyConfig,
    ) -> None:
        self.artifact = artifact
        self.history = history
        self.config = config

    def resolve(
        self,
        features: LatencyFeatures,
        *,
        catalog_ms: float | None,
        now: datetime,
    ) -> LatencyResolution:
        version = self.artifact.version if self.artifact else None
        if now.tzinfo is None or now.utcoffset() is None:
            return _catalog_resolution(catalog_ms, "predictor_error", version)
        if self.config.mode == "off":
            return _catalog_resolution(catalog_ms, "disabled", version)
        if self.artifact is None:
            return _catalog_resolution(catalog_ms, "no_artifact", None)

        try:
            return self._resolve_with_artifact(features, catalog_ms, now)
        except Exception:  # noqa: BLE001 - prediction is fail-open to the catalog path
            return _catalog_resolution(catalog_ms, "predictor_error", version)

    def _resolve_with_artifact(
        self,
        features: LatencyFeatures,
        catalog_ms: float | None,
        now: datetime,
    ) -> LatencyResolution:
        artifact = self.artifact
        assert artifact is not None
        evidence: dict[str, object] = {"model_key_hash": features.model_key.digest(), "profile_key": features.profile_key}
        snapshot_age = (now - self.history.built_at).total_seconds()
        if snapshot_age < 0 or snapshot_age > self.config.history_max_age_seconds:
            return _catalog_resolution(catalog_ms, "stale_history", artifact.version, evidence=evidence)
        if artifact.schema_version != "latency-v1":
            return _catalog_resolution(catalog_ms, "no_artifact", artifact.version, evidence=evidence)

        model_hash = features.model_key.digest()
        model_categories = artifact.categories.get("model_key_hash", ())
        support = artifact.support.get(features.profile_key)
        if model_hash not in model_categories:
            return _catalog_resolution(catalog_ms, "unseen_model", artifact.version, evidence=evidence)
        if support is None:
            return _catalog_resolution(catalog_ms, "unsupported_profile", artifact.version, evidence=evidence)
        if support.get("model_key_hash") != model_hash:
            return _catalog_resolution(catalog_ms, "unsupported_profile", artifact.version, evidence=evidence)
        for category_name in _CATEGORICAL_FEATURES:
            if _category_value(features, category_name) not in artifact.categories.get(category_name, ()):
                return _catalog_resolution(catalog_ms, "unsupported_profile", artifact.version, evidence=evidence)
        if not _coverage_ok(support, features):
            return _catalog_resolution(catalog_ms, "out_of_distribution", artifact.version, evidence=evidence)

        train_count = int(support.get("train_count", 0))
        validation_count = int(support.get("validation_count", 0))
        validation_profile = artifact.validation_report.get("profile_metrics", {}).get(features.profile_key, {})
        if (
            train_count < self.config.min_profile_train_samples
            or validation_count < self.config.min_profile_validation_samples
            or not bool(validation_profile.get("gate_pass", False))
        ):
            return _catalog_resolution(catalog_ms, "insufficient_samples", artifact.version, evidence=evidence)
        if not bool(artifact.validation_report.get("accepted", False)):
            return _catalog_resolution(catalog_ms, "high_error", artifact.version, evidence=evidence)

        stats = self.history.profiles.get(features.profile_key)
        if stats is None:
            return _catalog_resolution(catalog_ms, "insufficient_samples", artifact.version, evidence=evidence)
        if (
            stats.recent_success_count < self.config.min_profile_recent_successes
            or stats.newest_finished_at is None
            or (now - stats.newest_finished_at).total_seconds() < 0
            or (now - stats.newest_finished_at).total_seconds() > self.config.max_observation_age_seconds
        ):
            return _catalog_resolution(catalog_ms, "stale_history", artifact.version, evidence=evidence)
        if (
            stats.timeout_rate_30m is None
            or stats.censored_rate_30m is None
            or stats.timeout_rate_30m > self.config.max_timeout_fraction
            or stats.censored_rate_30m > self.config.max_censored_fraction
        ):
            return _catalog_resolution(catalog_ms, "high_failure_rate", artifact.version, evidence=evidence)
        if not _valid_positive(catalog_ms):
            return _catalog_resolution(catalog_ms, "unsupported_profile", artifact.version, evidence=evidence)

        base_log = _predict_log(artifact, features)
        if not math.isfinite(base_log) or base_log > 700:
            return _catalog_resolution(catalog_ms, "invalid_prediction", artifact.version, evidence=evidence)
        correction, residual_count = _residual_correction(stats, now, self.config)
        calibrated_log = base_log + correction
        predicted_ms = math.expm1(calibrated_log) if calibrated_log < 700 else math.inf
        evidence["residual_count"] = residual_count
        evidence["residual_correction_log"] = correction
        evidence["snapshot_age_seconds"] = snapshot_age
        evidence["train_count"] = train_count
        evidence["validation_count"] = validation_count
        evidence["recent_success_count"] = stats.recent_success_count
        if not _valid_positive(predicted_ms) or predicted_ms > self.config.max_latency_ms:
            return _catalog_resolution(catalog_ms, "invalid_prediction", artifact.version, evidence=evidence)
        if not _recent_metrics_ok(stats, now, self.config):
            return _catalog_resolution(
                catalog_ms,
                "high_error",
                artifact.version,
                predicted_ms=predicted_ms,
                evidence=evidence,
            )
        return LatencyResolution(predicted_ms, predicted_ms, "predicted", "auto", artifact.version, evidence)

    def resolve_many(
        self,
        features: Sequence[LatencyFeatures],
        *,
        catalog_by_model: Mapping[str, float | None],
        now: datetime,
    ) -> dict[str, LatencyResolution]:
        return {
            item.model_key.catalog_model_id: self.resolve(
                item,
                catalog_ms=catalog_by_model.get(item.model_key.catalog_model_id),
                now=now,
            )
            for item in features
        }
