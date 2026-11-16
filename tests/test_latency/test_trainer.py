import json
import math
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.backend_ai.latency.contracts import (
    LatencyArtifact,
    LatencyConfig,
    LatencyFeatures,
    LatencyObservation,
    ModelKey,
)
from src.backend_ai.latency.dataset import DatasetSnapshot, _digest_rows, write_dataset
from src.backend_ai.latency.trainer import (
    NUMERIC_FEATURES,
    evaluate_candidate,
    fit_latency_model,
    predict_base_ms,
    walk_forward_predict,
)

BASE = datetime(2026, 10, 14, tzinfo=UTC)
KEY_A = ModelKey("model-a", "provider", "upstream/a", "fingerprint", "openai")
KEY_B = ModelKey("model-b", "provider", "upstream/b", "fingerprint", "openai")


def _row(
    index: int,
    *,
    key: ModelKey = KEY_A,
    group: str = "train",
    actual: float | None = None,
    catalog_factor: float = 1.5,
    ewma_factor: float = 1.6,
    dispatch: float | None = None,
    finish: float | None = None,
) -> LatencyObservation:
    dispatch = float(index) if dispatch is None else dispatch
    finish = dispatch + 0.1 if finish is None else finish
    profile = f"profile-{key.catalog_model_id}"
    input_proxy = 100 + index * 13
    budget = 256 + (index % 4) * 128
    hour = (index % 24) / 24 * math.tau
    expected = 80 + 0.025 * input_proxy + 0.018 * budget + 5 * math.sin(hour)
    if key == KEY_B:
        expected += 120
    actual = expected if actual is None else actual
    values = {
        "model_key_hash": key.digest(),
        "provider_kind": key.provider_kind,
        "protocol": "openai_chat",
        "task_type": "general",
        "streaming": 0,
        "has_tools": 0,
        "has_schema": 0,
        "n": 1,
        "input_proxy": input_proxy,
        "output_budget": budget,
        "output_budget_missing": 0,
        "reasoning": 0.5,
        "reasoning_missing": 0,
        "risk": 0.0,
        "risk_missing": 0,
        "hour_sin": math.sin(hour),
        "hour_cos": math.cos(hour),
        "weekday_sin": math.sin(math.tau * (index % 7) / 7),
        "weekday_cos": math.cos(math.tau * (index % 7) / 7),
        "inter_arrival_ms": 1000,
        "inter_arrival_missing": 0,
        "measurement_age_ms": 100,
        "history_missing": 0,
        "ewma_ms": actual * ewma_factor,
        "median_ms": actual * ewma_factor,
        "p90_ms": actual * ewma_factor,
        "std_ms": 2.0,
        "error_rate_30m": 0.0,
        "timeout_rate_30m": 0.0,
        "success_count_30m": 10,
        "local_inflight": 0,
        "local_rpm": 1,
    }
    dispatched = BASE + timedelta(seconds=dispatch)
    finished = BASE + timedelta(seconds=finish)
    return LatencyObservation(
        f"{group}-{key.catalog_model_id}-{index}",
        f"{group}-{key.catalog_model_id}-request-{index}",
        "task",
        "app",
        LatencyFeatures(key, profile, dispatched, values),
        "openai_chat",
        "observe",
        dispatched,
        "success",
        finished,
        actual,
        catalog_ms=actual * catalog_factor,
    )


def _dataset(train_count: int = 160, validation_count: int = 120) -> DatasetSnapshot:
    train = tuple(_row(index, group="train") for index in range(train_count))
    validation = tuple(
        _row(index + train_count, group="validation") for index in range(validation_count)
    )
    digest = _digest_rows(train, validation)
    return DatasetSnapshot("latency-v1", BASE + timedelta(seconds=train_count + validation_count + 1), train, validation, digest)


def test_validation_rows_do_not_change_fit_coefficients_or_scaler():
    dataset = _dataset()
    altered_validation = tuple(
        _row(index + 160, group="validation", actual=90_000 + index, catalog_factor=1.0, ewma_factor=1.0)
        for index in range(120)
    )
    before = fit_latency_model(dataset, LatencyConfig())
    after = fit_latency_model(
        DatasetSnapshot(dataset.schema_version, dataset.cutoff, dataset.train_rows, altered_validation, dataset.digest),
        LatencyConfig(),
    )

    assert before.coefficients == after.coefficients
    assert before.numeric_means == after.numeric_means
    assert before.categories == after.categories


def test_model_identity_is_categorical_and_changes_prediction():
    train = tuple(_row(index, key=KEY_A) for index in range(120)) + tuple(
        _row(index, key=KEY_B) for index in range(120)
    )
    dataset = DatasetSnapshot("latency-v1", BASE + timedelta(days=1), train, (), "b" * 64)
    artifact = fit_latency_model(dataset, LatencyConfig())
    row_a = _row(200, key=KEY_A, group="validation")
    row_b = _row(200, key=KEY_B, group="validation")

    assert artifact.categories["model_key_hash"] == tuple(sorted((KEY_A.digest(), KEY_B.digest())))
    assert predict_base_ms(artifact, row_b.features) > predict_base_ms(artifact, row_a.features) * 1.5


def test_candidate_must_beat_catalog_and_ewma_on_common_validation_rows():
    dataset = _dataset()
    artifact = fit_latency_model(dataset, LatencyConfig())
    perfect_baseline_rows = tuple(
        _row(index + 160, group="validation", catalog_factor=1.0, ewma_factor=1.0)
        for index in range(120)
    )
    validation = DatasetSnapshot(dataset.schema_version, dataset.cutoff, dataset.train_rows, perfect_baseline_rows, dataset.digest)

    report = evaluate_candidate(artifact, validation, LatencyConfig())

    assert report.accepted is False
    assert "does_not_beat_catalog" in report.reasons or "does_not_beat_ewma" in report.reasons


def test_common_validation_minimum_comes_from_typed_config():
    dataset = _dataset(validation_count=20)
    artifact = fit_latency_model(dataset, LatencyConfig())

    enough = evaluate_candidate(artifact, dataset, LatencyConfig(min_common_validation_samples=20))
    too_few = evaluate_candidate(artifact, dataset, LatencyConfig(min_common_validation_samples=21))

    assert "insufficient_common_validation_rows" not in enough.reasons
    assert "insufficient_common_validation_rows" in too_few.reasons


def test_walk_forward_never_uses_outcome_that_finishes_after_prediction_time():
    artifact = LatencyArtifact(
        "latency-v1",
        "constant",
        tuple(f"num:{name}" for name in NUMERIC_FEATURES),
        tuple(0.0 for _ in NUMERIC_FEATURES),
        math.log1p(100),
        numeric_means={name: 0.0 for name in NUMERIC_FEATURES},
        numeric_scales={name: 1.0 for name in NUMERIC_FEATURES},
        categories={"model_key_hash": (), "provider_kind": (), "protocol": (), "task_type": ()},
    )
    rows = [
        _row(index, group="validation", actual=201.0, dispatch=float(index), finish=float(index) + 0.1)
        for index in range(20)
    ]
    rows.append(_row(20, group="validation", actual=50.0, dispatch=20.0, finish=24.0))
    rows.append(_row(21, group="validation", actual=201.0, dispatch=21.0, finish=22.0))

    predictions = walk_forward_predict(artifact, rows, LatencyConfig())
    by_id = {row.attempt_id: row for row in predictions}
    b = by_id["validation-model-a-21"]

    assert b.residual_count_before == 20
    assert b.prediction_ms == pytest.approx(201.0, rel=1e-6)


def test_child_cli_writes_private_atomic_result_without_raw_prompt_or_secret(tmp_path: Path):
    dataset_path = tmp_path / "dataset.json"
    options_path = tmp_path / "options.json"
    result_path = tmp_path / "result.json"
    dataset = _dataset()
    write_dataset(dataset, dataset_path)
    options_path.write_text(json.dumps(asdict(LatencyConfig())), encoding="utf-8")
    options_path.chmod(0o600)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.backend_ai.latency.trainer",
            "--dataset",
            str(dataset_path),
            "--result",
            str(result_path),
            "--options",
            str(options_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    result_bytes = result_path.read_bytes()
    result = json.loads(result_bytes)
    assert result["dataset_digest"] == dataset.digest
    assert result["artifact"]["schema_version"] == "latency-v1"
    assert os.stat(result_path).st_mode & 0o777 == 0o600
    assert b"raw prompt" not in result_bytes
    assert b"api_key" not in result_bytes
    assert not list(tmp_path.glob(".result.json.*.tmp"))
