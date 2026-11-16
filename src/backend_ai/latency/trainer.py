from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from src.backend_ai.latency.contracts import LatencyArtifact, LatencyConfig, LatencyFeatures, LatencyObservation
from src.backend_ai.latency.dataset import DatasetSnapshot, read_dataset
from src.backend_ai.latency.history import is_usable_latency_label
from src.backend_ai.latency.store import artifact_to_payload, sign_artifact, validate_artifact

CATEGORICAL_FEATURES = ("model_key_hash", "provider_kind", "protocol", "task_type")
NUMERIC_FEATURES = (
    "streaming",
    "has_tools",
    "has_schema",
    "n",
    "log_input_proxy",
    "log_output_budget",
    "output_budget_missing",
    "reasoning",
    "reasoning_missing",
    "risk",
    "risk_missing",
    "hour_sin",
    "hour_cos",
    "weekday_sin",
    "weekday_cos",
    "log_inter_arrival_ms",
    "inter_arrival_missing",
    "log_measurement_age_ms",
    "history_missing",
    "log_ewma_ms",
    "log_median_ms",
    "log_p90_ms",
    "log_std_ms",
    "error_rate_30m",
    "timeout_rate_30m",
    "log_success_count_30m",
    "log_local_inflight",
    "log_local_rpm",
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


@dataclass(frozen=True, slots=True)
class ValidationReport:
    accepted: bool
    reasons: tuple[str, ...]
    global_metrics: Mapping[str, float]
    profile_metrics: Mapping[str, Mapping[str, object]]

    def to_dict(self) -> dict[str, Any]:
        def finite(value):
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if isinstance(value, Mapping):
                return {key: finite(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [finite(item) for item in value]
            return value

        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "global_metrics": finite(self.global_metrics),
            "profile_metrics": finite(self.profile_metrics),
        }


@dataclass(frozen=True, slots=True)
class WalkForwardPrediction:
    attempt_id: str
    prediction_ms: float
    correction_log: float
    residual_count_before: int


def _numeric_values(values: Mapping[str, object]) -> list[float]:
    result: list[float] = []
    for feature_name in NUMERIC_FEATURES:
        raw_name = _LOG_FEATURES.get(feature_name, feature_name)
        raw = values.get(raw_name)
        if raw is None:
            result.append(0.0)
            continue
        if not isinstance(raw, (bool, int, float)):
            raise ValueError(f"numeric feature {raw_name} has a non-numeric value")
        number = float(raw)
        if not math.isfinite(number):
            raise ValueError(f"numeric feature {raw_name} is non-finite")
        if feature_name in _LOG_FEATURES:
            if number < 0:
                raise ValueError(f"log feature {raw_name} must be non-negative")
            number = math.log1p(number)
        result.append(number)
    return result


def _category(values: Mapping[str, object], feature_name: str) -> str:
    value = values.get(feature_name)
    return str(value) if value is not None and str(value) else "unknown"


def _rows_for_training(rows: Sequence[LatencyObservation]) -> tuple[LatencyObservation, ...]:
    return tuple(row for row in rows if is_usable_latency_label(row))


def _selected_model_groups(rows: Sequence[LatencyObservation], max_groups: int) -> tuple[str, ...]:
    counts = Counter(row.features.model_key.digest() for row in rows)
    return tuple(key for key, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:max_groups])


def _category_schema(rows: Sequence[LatencyObservation], allowed_models: set[str]) -> dict[str, tuple[str, ...]]:
    filtered = [row for row in rows if row.features.model_key.digest() in allowed_models]
    result = {
        name: tuple(sorted({_category(row.features.values, name) for row in filtered}))
        for name in CATEGORICAL_FEATURES
    }
    return {name: categories or ("unknown",) for name, categories in result.items()}


def _matrix(
    rows: Sequence[LatencyObservation],
    categories: Mapping[str, Sequence[str]],
    means: Mapping[str, float],
    scales: Mapping[str, float],
):
    import numpy as np

    category_offsets: dict[tuple[str, str], int] = {}
    feature_count = 0
    for name in CATEGORICAL_FEATURES:
        for value in categories[name]:
            category_offsets[(name, value)] = feature_count
            feature_count += 1
    numeric_offset = feature_count
    matrix = np.zeros((len(rows), feature_count + len(NUMERIC_FEATURES)), dtype=np.float64)
    for row_index, row in enumerate(rows):
        values = row.features.values
        for name in CATEGORICAL_FEATURES:
            category_index = category_offsets.get((name, _category(values, name)))
            if category_index is not None:
                matrix[row_index, category_index] = 1.0
        numeric = _numeric_values(values)
        for index, (feature_name, value) in enumerate(zip(NUMERIC_FEATURES, numeric, strict=True)):
            matrix[row_index, numeric_offset + index] = (value - means[feature_name]) / scales[feature_name]
    return matrix


def _feature_names(categories: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    category_names = tuple(
        f"cat:{name}={value}"
        for name in CATEGORICAL_FEATURES
        for value in categories[name]
    )
    return category_names + tuple(f"num:{name}" for name in NUMERIC_FEATURES)


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil((len(ordered) - 1) * fraction))
    return ordered[index]


def _build_support(
    train_rows: Sequence[LatencyObservation], validation_rows: Sequence[LatencyObservation], allowed_models: set[str]
) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[LatencyObservation]] = defaultdict(list)
    validation_counts: Counter[str] = Counter()
    for row in train_rows:
        if row.features.model_key.digest() in allowed_models:
            grouped[row.features.profile_key].append(row)
    for row in validation_rows:
        validation_counts[row.features.profile_key] += 1
    profiles = set(grouped) | set(validation_counts)
    support: dict[str, dict[str, object]] = {}
    for profile in sorted(profiles):
        rows = grouped.get(profile, [])
        inputs = [float(row.features.values["input_proxy"]) for row in rows
                  if isinstance(row.features.values.get("input_proxy"), (int, float))]
        budgets = [float(row.features.values["output_budget"]) for row in rows
                   if isinstance(row.features.values.get("output_budget"), (int, float))]
        support[profile] = {
            "model_key_hash": rows[0].features.model_key.digest() if rows else None,
            "train_count": len(rows),
            "validation_count": int(validation_counts[profile]),
            "input_proxy_p05": _percentile(inputs, 0.05),
            "input_proxy_p95": _percentile(inputs, 0.95),
            "output_budget_p05": _percentile(budgets, 0.05),
            "output_budget_p95": _percentile(budgets, 0.95),
            "train_missing_budget": sum(row.features.values.get("output_budget") is None for row in rows),
            "validation_missing_budget": sum(
                row.features.values.get("output_budget") is None
                for row in validation_rows if row.features.profile_key == profile
            ),
        }
    return support


def fit_latency_model(dataset: DatasetSnapshot, config: LatencyConfig) -> LatencyArtifact:
    import numpy as np

    rows = list(_rows_for_training(dataset.train_rows))[-config.max_training_rows :]
    if not rows:
        raise ValueError("dataset has no usable training labels")
    selected = _selected_model_groups(rows, config.max_model_groups)
    allowed = set(selected)
    categories = _category_schema(rows, allowed)
    model_categories = list(categories["model_key_hash"])
    category_width = sum(len(categories[name]) for name in CATEGORICAL_FEATURES)
    while category_width + len(NUMERIC_FEATURES) > config.max_features and len(model_categories) > 1:
        counts = Counter(row.features.model_key.digest() for row in rows)
        weakest = sorted(model_categories, key=lambda key: (counts.get(key, 0), key))[0]
        allowed.discard(weakest)
        model_categories.remove(weakest)
        categories["model_key_hash"] = tuple(model_categories)
        category_width = sum(len(categories[name]) for name in CATEGORICAL_FEATURES)
    if category_width + len(NUMERIC_FEATURES) > config.max_features:
        raise ValueError("latency feature schema exceeds configured feature limit")
    rows = [row for row in rows if row.features.model_key.digest() in allowed]
    if not rows:
        raise ValueError("no supported model groups remain after feature limiting")

    raw_numeric = np.asarray([_numeric_values(row.features.values) for row in rows], dtype=np.float64)
    means_array = raw_numeric.mean(axis=0)
    scales_array = raw_numeric.std(axis=0)
    scales_array[scales_array <= 1e-12] = 1.0
    means = {name: float(means_array[index]) for index, name in enumerate(NUMERIC_FEATURES)}
    scales = {name: float(scales_array[index]) for index, name in enumerate(NUMERIC_FEATURES)}
    x_values = _matrix(rows, categories, means, scales)
    design = np.column_stack((np.ones(len(rows), dtype=np.float64), x_values))
    target = np.asarray([math.log1p(float(row.provider_total_ms)) for row in rows], dtype=np.float64)
    ages = np.asarray(
        [max(0.0, (dataset.cutoff - row.finished_at).total_seconds()) for row in rows], dtype=np.float64
    )
    sample_weights = np.exp(-ages / config.training_feature_decay_seconds)
    root_weights = np.sqrt(sample_weights)
    weighted_design = design * root_weights[:, None]
    weighted_target = target * root_weights
    penalty = np.eye(design.shape[1], dtype=np.float64)
    penalty[0, 0] = 0.0
    gram = weighted_design.T @ weighted_design + config.ridge_alpha * penalty
    coefficients = np.linalg.solve(gram, weighted_design.T @ weighted_target)
    if not np.all(np.isfinite(coefficients)):
        raise ValueError("ridge produced non-finite coefficients")

    feature_names = _feature_names(categories)
    version = f"latency-v1-{dataset.digest[:12]}"
    support = _build_support(rows, dataset.validation_rows, allowed)
    artifact = LatencyArtifact(
        schema_version="latency-v1",
        version=version,
        feature_names=feature_names,
        coefficients=tuple(float(value) for value in coefficients[1:]),
        intercept=float(coefficients[0]),
        numeric_means=means,
        numeric_scales=scales,
        categories=categories,
        support=support,
        validation_report={"train_count": len(rows), "dataset_digest": dataset.digest},
    )
    return replace(artifact, checksum=sign_artifact(artifact_to_payload(artifact))["checksum"])


def _encoded_values(artifact: LatencyArtifact, features: LatencyFeatures) -> list[float]:
    values = features.values
    output: list[float] = []
    for name in CATEGORICAL_FEATURES:
        category = _category(values, name)
        output.extend(1.0 if category == value else 0.0 for value in artifact.categories.get(name, ()))
    numeric = _numeric_values(values)
    for index, name in enumerate(NUMERIC_FEATURES):
        output.append((numeric[index] - float(artifact.numeric_means[name])) / float(artifact.numeric_scales[name]))
    return output


def _predict_log(artifact: LatencyArtifact, features: LatencyFeatures) -> float:
    values = _encoded_values(artifact, features)
    if len(values) != len(artifact.coefficients):
        raise ValueError("artifact feature dimension mismatch")
    return artifact.intercept + sum(weight * value for weight, value in zip(artifact.coefficients, values, strict=True))


def predict_base_ms(artifact: LatencyArtifact, features: LatencyFeatures) -> float:
    log_prediction = _predict_log(artifact, features)
    if not math.isfinite(log_prediction) or log_prediction > 700:
        return float("inf")
    return max(1.0, math.expm1(log_prediction))


def _residual_correction(
    residuals: Sequence[tuple[datetime, float]],
    as_of: datetime,
    config: LatencyConfig,
) -> tuple[float, int]:
    if len(residuals) < config.min_residual_samples:
        return 0.0, len(residuals)
    weighted = total = 0.0
    for finished_at, value in residuals:
        age = max(0.0, (as_of - finished_at).total_seconds())
        weight = math.exp(-math.log(2) * age / config.residual_half_life_seconds)
        weighted += value * weight
        total += weight
    correction = weighted / total if total else 0.0
    return max(-config.residual_clip_log, min(config.residual_clip_log, correction)), len(residuals)


def walk_forward_predict(
    artifact: LatencyArtifact,
    rows: Sequence[LatencyObservation],
    config: LatencyConfig,
) -> tuple[WalkForwardPrediction, ...]:
    ordered = sorted(
        (row for row in rows if is_usable_latency_label(row)),
        key=lambda row: (row.dispatched_at, row.attempt_id),
    )
    pending: list[tuple[datetime, float]] = []
    residuals: list[tuple[datetime, float]] = []
    predictions: list[WalkForwardPrediction] = []
    for row in ordered:
        still_pending = []
        for finished_at, residual in pending:
            if finished_at <= row.dispatched_at:
                residuals.append((finished_at, residual))
            else:
                still_pending.append((finished_at, residual))
        pending = still_pending
        if len(residuals) > 200:
            residuals = residuals[-200:]
        correction, count = _residual_correction(residuals, row.dispatched_at, config)
        base_log = _predict_log(artifact, row.features)
        calibrated_log = base_log + correction
        predicted = max(1.0, math.expm1(calibrated_log)) if calibrated_log < 700 else float("inf")
        predictions.append(WalkForwardPrediction(row.attempt_id, predicted, correction, count))
        actual = float(row.provider_total_ms)
        if math.isfinite(predicted) and predicted > 0 and actual > 0:
            residual = math.log1p(actual) - math.log1p(predicted)
            pending.append((row.finished_at, residual))
    return tuple(predictions)


def _errors(predicted: Sequence[float], actual: Sequence[float]) -> tuple[float, float]:
    if not predicted:
        return math.inf, math.inf
    absolute = [abs(p - y) for p, y in zip(predicted, actual, strict=True)]
    mae = sum(absolute) / len(absolute)
    total = sum(actual)
    wape = sum(absolute) / total if total > 0 else math.inf
    return mae, wape


def evaluate_candidate(
    artifact: LatencyArtifact,
    dataset: DatasetSnapshot,
    config: LatencyConfig,
) -> ValidationReport:
    predictions = {item.attempt_id: item for item in walk_forward_predict(artifact, dataset.validation_rows, config)}
    common: list[tuple[LatencyObservation, float, float, float]] = []
    all_predictions: list[tuple[LatencyObservation, float]] = []
    for row in dataset.validation_rows:
        prediction = predictions.get(row.attempt_id)
        if prediction is None:
            continue
        all_predictions.append((row, prediction.prediction_ms))
        catalog = row.catalog_ms
        ewma = row.features.values.get("ewma_ms")
        actual = row.provider_total_ms
        if (
            actual is not None and catalog is not None and isinstance(ewma, (int, float))
            and all(math.isfinite(float(value)) and float(value) > 0 for value in (actual, catalog, ewma))
            and math.isfinite(prediction.prediction_ms) and 0 < prediction.prediction_ms <= config.max_latency_ms
        ):
            common.append((row, prediction.prediction_ms, float(actual), float(catalog)))
    candidate_mae, candidate_wape = _errors([p for _, p, _, _ in common], [y for _, _, y, _ in common])
    catalog_mae, _ = _errors([catalog for _, _, _, catalog in common], [y for _, _, y, _ in common])
    ewma_mae, _ = _errors(
        [float(row.features.values["ewma_ms"]) for row, _, _, _ in common],
        [y for _, _, y, _ in common],
    )
    global_metrics = {
        "validation_count": float(len(dataset.validation_rows)),
        "common_count": float(len(common)),
        "candidate_mae_ms": candidate_mae,
        "candidate_wape": candidate_wape,
        "catalog_mae_ms": catalog_mae,
        "ewma_mae_ms": ewma_mae,
    }
    grouped: dict[str, list[tuple[LatencyObservation, float, float, float]]] = defaultdict(list)
    for item in common:
        grouped[item[0].features.profile_key].append(item)
    profile_metrics: dict[str, dict[str, object]] = {}
    any_profile_good = False
    for profile in sorted(set(grouped) | set(artifact.support)):
        rows = grouped.get(profile, [])
        profile_support = artifact.support.get(profile, {})
        pred = [item[1] for item in rows]
        actual = [item[2] for item in rows]
        catalog_values = [item[3] for item in rows]
        ewma_values = [float(item[0].features.values["ewma_ms"]) for item in rows]
        profile_mae, profile_wape = _errors(pred, actual)
        profile_catalog_mae, _ = _errors(catalog_values, actual)
        profile_ewma_mae, _ = _errors(ewma_values, actual)
        train_count = int(profile_support.get("train_count", 0))
        validation_count = len(rows)
        good = (
            train_count >= config.min_profile_train_samples
            and validation_count >= config.min_profile_validation_samples
            and profile_wape <= config.max_wape
            and profile_mae <= config.max_candidate_catalog_mae_ratio * profile_catalog_mae
            and profile_mae <= profile_ewma_mae
        )
        any_profile_good = any_profile_good or good
        profile_metrics[profile] = {
            "train_count": train_count,
            "validation_count": validation_count,
            "candidate_mae_ms": profile_mae,
            "candidate_wape": profile_wape,
            "catalog_mae_ms": profile_catalog_mae,
            "ewma_mae_ms": profile_ewma_mae,
            "gate_pass": good,
        }

    reasons = []
    if len(common) < config.min_common_validation_samples:
        reasons.append("insufficient_common_validation_rows")
    if not math.isfinite(candidate_wape) or candidate_wape > config.max_wape:
        reasons.append("high_wape")
    if not math.isfinite(catalog_mae) or candidate_mae > config.max_publish_catalog_mae_ratio * catalog_mae:
        reasons.append("does_not_beat_catalog")
    if not math.isfinite(ewma_mae) or candidate_mae > ewma_mae:
        reasons.append("does_not_beat_ewma")
    if not any_profile_good:
        reasons.append("no_supported_profile")
    return ValidationReport(not reasons, tuple(reasons), global_metrics, profile_metrics)


def _write_result(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def train_snapshot(dataset_path: Path, result_path: Path, options_path: Path) -> None:
    options = json.loads(Path(options_path).read_text(encoding="utf-8"))
    if not isinstance(options, dict):
        raise ValueError("training options must be a JSON object")
    config = LatencyConfig(**options)
    dataset = read_dataset(dataset_path)
    candidate = fit_latency_model(dataset, config)
    report = evaluate_candidate(candidate, dataset, config)
    candidate = replace(candidate, validation_report=report.to_dict(), checksum="")
    candidate = validate_artifact(sign_artifact(artifact_to_payload(candidate)))
    _write_result(
        Path(result_path),
        {
            "accepted": report.accepted,
            "artifact": artifact_to_payload(candidate),
            "report": report.to_dict(),
            "dataset_digest": dataset.digest,
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fit and evaluate a latency-v1 artifact from a recorded dataset")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--options", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        train_snapshot(args.dataset, args.result, args.options)
    except Exception as exc:  # noqa: BLE001 - child exits nonzero; caller keeps last-good artifact
        parser.exit(2, f"latency trainer failed: {type(exc).__name__}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
