from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from src.backend_ai.latency.contracts import (
    HistorySnapshot,
    LatencyArtifact,
    LatencyConfig,
    LatencyFeatures,
    ModelKey,
    ProfileStats,
)
from src.backend_ai.latency.trainer import NUMERIC_FEATURES

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
KEY = ModelKey("model-a", "provider", "upstream/a", "connection-a", "openai")
OTHER_CONNECTION = ModelKey("model-a", "provider", "upstream/a", "connection-b", "openai")
PROFILE = "profile-a"


def _features(
    *,
    key: ModelKey = KEY,
    profile: str = PROFILE,
    input_proxy: int = 100,
    output_budget: int | None = 500,
    task_type: str = "general",
):
    return LatencyFeatures(
        key,
        profile,
        NOW,
        {
            "model_key_hash": key.digest(),
            "provider_kind": key.provider_kind,
            "protocol": "openai_chat",
            "task_type": task_type,
            "streaming": 0,
            "has_tools": 0,
            "has_schema": 0,
            "n": 1,
            "input_proxy": input_proxy,
            "output_budget": output_budget,
            "output_budget_missing": int(output_budget is None),
            "reasoning": 0.5,
            "reasoning_missing": 0,
            "risk": 0.0,
            "risk_missing": 0,
            "hour_sin": 0.0,
            "hour_cos": 1.0,
            "weekday_sin": 0.0,
            "weekday_cos": 1.0,
            "inter_arrival_ms": 1000,
            "inter_arrival_missing": 0,
            "measurement_age_ms": 100,
            "history_missing": 0,
            "ewma_ms": 120,
            "median_ms": 120,
            "p90_ms": 150,
            "std_ms": 5,
            "error_rate_30m": 0.0,
            "timeout_rate_30m": 0.0,
            "success_count_30m": 5,
            "local_inflight": 0,
            "local_rpm": 1,
        },
    )


def _artifact(*, profile: str = PROFILE, support: dict | None = None, report: dict | None = None):
    categories = {
        "model_key_hash": (KEY.digest(),),
        "provider_kind": (KEY.provider_kind,),
        "protocol": ("openai_chat",),
        "task_type": ("general",),
    }
    support = support or {
        profile: {
            "model_key_hash": KEY.digest(),
            "train_count": 60,
            "validation_count": 25,
            "input_proxy_p05": 50,
            "input_proxy_p95": 150,
            "output_budget_p05": 100,
            "output_budget_p95": 1000,
            "train_missing_budget": 0,
            "validation_missing_budget": 0,
        }
    }
    report = report or {
        "accepted": True,
        "global_metrics": {"candidate_wape": 0.2},
        "profile_metrics": {
            profile: {
                "gate_pass": True,
                "validation_count": 25,
                "candidate_wape": 0.2,
            }
        },
    }
    feature_names = tuple(
        [f"cat:{name}={value}" for name in categories for value in categories[name]]
        + [f"num:{name}" for name in NUMERIC_FEATURES]
    )
    coefficients = [0.0] * len(feature_names)
    return LatencyArtifact(
        "latency-v1",
        "latency-v1-test",
        feature_names,
        tuple(coefficients),
        math.log1p(100),
        {name: 0.0 for name in NUMERIC_FEATURES},
        {name: 1.0 for name in NUMERIC_FEATURES},
        categories,
        support,
        report,
    )


def _history(
    *,
    built_at: datetime = NOW,
    stats: ProfileStats | None = None,
    profile: str = PROFILE,
):
    stats = stats or ProfileStats(
        train_count=60,
        recent_success_count=60,
        recent_count=60,
        newest_finished_at=NOW - timedelta(minutes=5),
        timeout_rate_30m=0.0,
        censored_rate_30m=0.0,
        error_rate_30m=0.0,
        recent_pairs=(),
        residuals=(),
    )
    return HistorySnapshot(built_at, {profile: stats})


def _resolver(*, mode="auto", artifact=None, history=None, config=None):
    from src.backend_ai.latency.predictor import LatencyResolver

    return LatencyResolver(
        artifact=artifact or _artifact(),
        history=history or _history(),
        config=config or LatencyConfig(mode=mode),
    )


def test_unseen_model_and_rotated_connection_use_catalog_even_with_large_global_history():
    resolver = _resolver()
    unseen = resolver.resolve(_features(key=OTHER_CONNECTION), catalog_ms=900, now=NOW)

    assert unseen.source == "catalog"
    assert unseen.effective_ms == 900
    assert unseen.gate_reason == "unseen_model"


def test_valid_exact_profile_in_auto_mode_uses_prediction():
    result = _resolver().resolve(_features(), catalog_ms=900, now=NOW)

    assert result.source == "predicted"
    assert result.effective_ms == pytest.approx(100)
    assert result.model_version == "latency-v1-test"


def test_auto_can_serve_last_good_artifact_while_training_is_paused():
    resolver = _resolver(config=LatencyConfig(mode="auto", training_enabled=False))

    result = resolver.resolve(_features(), catalog_ms=900, now=NOW)

    assert result.source == "predicted"
    assert result.gate_reason == "auto"


def test_auto_without_artifact_uses_catalog():
    from src.backend_ai.latency.predictor import LatencyResolver

    result = LatencyResolver(None, _history(), LatencyConfig(mode="auto")).resolve(
        _features(), catalog_ms=900, now=NOW
    )

    assert result.source == "catalog"
    assert result.effective_ms == 900
    assert result.gate_reason == "no_artifact"


@pytest.mark.parametrize(
    ("mode", "expected_source", "expected_effective", "expected_reason"),
    [
        ("off", "catalog", 900, "disabled"),
    ],
)
def test_off_mode_uses_catalog(mode, expected_source, expected_effective, expected_reason):
    result = _resolver(mode=mode).resolve(_features(), catalog_ms=900, now=NOW)

    assert result.source == expected_source
    assert result.effective_ms == expected_effective
    assert result.predicted_ms is None
    assert result.gate_reason == expected_reason


def test_unknown_catalog_never_becomes_zero():
    result = _resolver().resolve(_features(), catalog_ms=None, now=NOW)

    assert result.source == "unknown"
    assert result.effective_ms is None
    assert result.gate_reason == "unsupported_profile"


@pytest.mark.parametrize(
    ("history", "expected_reason"),
    [
        (_history(built_at=NOW - timedelta(seconds=61)), "stale_history"),
        (
            _history(stats=ProfileStats(
                train_count=60, recent_success_count=60, recent_count=60,
                newest_finished_at=NOW - timedelta(seconds=1801), timeout_rate_30m=0,
                censored_rate_30m=0, error_rate_30m=0,
            )),
            "stale_history",
        ),
        (
            _history(stats=ProfileStats(
                train_count=60, recent_success_count=60, recent_count=100,
                newest_finished_at=NOW - timedelta(minutes=1), timeout_rate_30m=0.06,
                censored_rate_30m=0.06, error_rate_30m=0,
            )),
            "high_failure_rate",
        ),
    ],
)
def test_history_freshness_and_failure_rates_are_gates(history, expected_reason):
    result = _resolver(history=history).resolve(_features(), catalog_ms=900, now=NOW)
    assert result.source == "catalog"
    assert result.gate_reason == expected_reason


@pytest.mark.parametrize(
    ("features", "expected_reason"),
    [
        (_features(input_proxy=10_000), "out_of_distribution"),
        (_features(profile="different-profile"), "unsupported_profile"),
        (_features(task_type="unknown-new-task"), "unsupported_profile"),
        (_features(output_budget=None), "out_of_distribution"),
    ],
)
def test_profile_and_workload_must_be_supported(features, expected_reason):
    result = _resolver().resolve(features, catalog_ms=900, now=NOW)
    assert result.source == "catalog"
    assert result.gate_reason == expected_reason


def test_profile_sample_counts_and_quality_report_are_required():
    artifact = _artifact(support={PROFILE: {
        "model_key_hash": KEY.digest(), "train_count": 49, "validation_count": 25,
        "input_proxy_p05": 50, "input_proxy_p95": 150,
        "output_budget_p05": 100, "output_budget_p95": 1000,
    }})
    result = _resolver(artifact=artifact).resolve(_features(), catalog_ms=900, now=NOW)
    assert result.gate_reason == "insufficient_samples"


def test_recent_error_can_disable_previously_valid_profile():
    pairs = tuple({
        "attempt_id": f"recent-{index}",
        "finished_at": (NOW - timedelta(seconds=index)).isoformat(),
        "predicted_ms": 100.0,
        "actual_ms": 200.0,
        "catalog_ms": 100.0,
        "ewma_ms": 100.0,
    } for index in range(20))
    stats = ProfileStats(
        train_count=60,
        recent_success_count=60,
        recent_count=60,
        newest_finished_at=NOW - timedelta(minutes=1),
        timeout_rate_30m=0,
        censored_rate_30m=0,
        error_rate_30m=0,
        recent_pairs=pairs,
    )
    result = _resolver(history=_history(stats=stats)).resolve(_features(), catalog_ms=900, now=NOW)

    assert result.source == "catalog"
    assert result.predicted_ms == pytest.approx(100)
    assert result.gate_reason == "high_error"


def test_recent_residuals_are_versioned_as_of_and_clamped():
    stats = ProfileStats(
        train_count=60,
        recent_success_count=60,
        recent_count=60,
        newest_finished_at=NOW - timedelta(minutes=1),
        timeout_rate_30m=0,
        censored_rate_30m=0,
        error_rate_30m=0,
        residuals=tuple({
            "finished_at": (NOW - timedelta(seconds=30 - index)).isoformat(),
            "residual_log": 3.0,
        } for index in range(20)) + ({"finished_at": (NOW + timedelta(seconds=5)).isoformat(), "residual_log": 4.0},),
    )
    result = _resolver(history=_history(stats=stats)).resolve(_features(), catalog_ms=900, now=NOW)

    assert result.predicted_ms == pytest.approx(201, rel=0.01)


def test_zero_span_coverage_uses_relative_tolerance():
    artifact = _artifact(support={PROFILE: {
        "model_key_hash": KEY.digest(), "train_count": 60, "validation_count": 25,
        "input_proxy_p05": 1000, "input_proxy_p95": 1000,
        "output_budget_p05": 500, "output_budget_p95": 500,
        "train_missing_budget": 0, "validation_missing_budget": 0,
    }})
    accepted = _resolver(artifact=artifact).resolve(_features(input_proxy=1250, output_budget=500), catalog_ms=900, now=NOW)
    rejected = _resolver(artifact=artifact).resolve(_features(input_proxy=1400, output_budget=500), catalog_ms=900, now=NOW)

    assert accepted.source == "predicted"
    assert rejected.gate_reason == "out_of_distribution"


def test_resolve_many_keys_results_by_catalog_model_id():
    results = _resolver().resolve_many([_features()], catalog_by_model={"model-a": 900}, now=NOW)
    assert results["model-a"].source == "predicted"
