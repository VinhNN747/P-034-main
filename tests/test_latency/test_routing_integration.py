from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.backend_ai.gateway.providers.base import GenerationOptions
from src.backend_ai.latency.context import LatencyRequestContext, latency_request_scope
from src.backend_ai.latency.contracts import (
    HistorySnapshot,
    LatencyArtifact,
    LatencyConfig,
    LatencyFeatures,
    ModelKey,
    ProfileStats,
)
from src.backend_ai.latency.features import build_features
from src.backend_ai.latency.predictor import LatencyResolver
from src.backend_ai.services.gateway_routing_service import (
    DatabaseRoutingService,
    DatabaseRoutingSnapshot,
    _Candidate,
)
from src.shared.dtos.gateway import Difficulty, QualityPriority, RequestConstraints, SubTask, TaskType
from src.shared.repositories.routing_catalog import RoutingSet

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def _task():
    return SubTask(
        task_id="task-1",
        description="classify the request",
        task_type=TaskType.CLASSIFICATION,
        difficulty=Difficulty.MEDIUM,
        required_capabilities=["chat"],
        expected_output="label",
    )


def _candidate(model_id: str, latency: int, *, price: float = 1.0):
    model = SimpleNamespace(
        id=model_id,
        name=model_id,
        input_price_per_million=price,
        output_price_per_million=0.0,
        pricing_version="test",
        target_latency_ms=latency,
        quality_tier="mid",
        capabilities_json=["chat"],
        status="active",
        context_window=None,
    )
    provider = SimpleNamespace(
        id="local-provider", name="local", kind="offline", purpose="target", status="active"
    )
    return _Candidate(model, provider)


@pytest.mark.parametrize("priority", list(QualityPriority))
def test_latency_override_uses_existing_objective_weights(priority):
    candidates = [_candidate("z-fast-catalog", 100), _candidate("a-slow-catalog", 900)]
    base = DatabaseRoutingService._rank_candidates(candidates, _task(), priority, {})
    learned = DatabaseRoutingService._rank_candidates(
        candidates,
        _task(),
        priority,
        {},
        latency_overrides={"z-fast-catalog": 900, "a-slow-catalog": 100},
    )

    assert base[0].model.id == "z-fast-catalog"
    assert learned[0].model.id == "a-slow-catalog"


def test_primary_role_stays_ahead_of_a_faster_fallback():
    primary = _candidate("primary", 900)
    fallback = _candidate("fallback", 100)

    result = DatabaseRoutingService._primary_first(
        [fallback, primary],
        {"primary": "primary", "fallback": "fallback"},
        {"primary": 2.0, "fallback": 2.0},
    )

    assert result[0].model.id == "primary"


def test_catalog_max_latency_still_controls_eligibility():
    model = SimpleNamespace(
        id="slow", name="slow", status="active", target_latency_ms=900, capabilities_json=["chat"]
    )
    provider = SimpleNamespace(id="provider", name="provider", kind="openai", purpose="target", status="active")

    assert not DatabaseRoutingService._eligible(
        model, provider, _task(), RequestConstraints(max_latency_ms=100)
    )


def test_null_catalog_latency_is_eligible_without_cap_but_never_passes_a_latency_cap():
    model = SimpleNamespace(id="anthropic", name="claude-haiku-5-5", status="active", target_latency_ms=None,
        capabilities_json=["chat"])
    provider = SimpleNamespace(id="anthropic-provider", name="anthropic", kind="anthropic", purpose="target",
        status="active")

    assert DatabaseRoutingService._eligible(model, provider, _task(), RequestConstraints())
    assert not DatabaseRoutingService._eligible(model, provider, _task(), RequestConstraints(max_latency_ms=3000))


def _routing_state():
    cheap_id, quality_id = "cheap-model", "quality-model"
    keys = {
        cheap_id: ModelKey(cheap_id, "local-provider", "cheap-fast", "offline-connection", "offline"),
        quality_id: ModelKey(quality_id, "local-provider", "quality-reasoner", "offline-connection", "offline"),
    }
    empty_history = HistorySnapshot(NOW, {})
    options = GenerationOptions(max_output_tokens=1000, hard_completion_token_limit=1000)
    features: dict[str, LatencyFeatures] = {
        model_id: build_features(
            key,
            material_chars=300,
            options=options,
            task_type="classification",
            signals=None,
            now=NOW,
            history=empty_history,
            local_load={"inflight": 0, "rpm": 1},
            protocol="native",
            streaming=False,
        )
        for model_id, key in keys.items()
    }
    profile_stats = {
        feature.profile_key: ProfileStats(
            train_count=60,
            recent_success_count=60,
            recent_count=60,
            newest_finished_at=NOW - timedelta(minutes=1),
            timeout_rate_30m=0,
            censored_rate_30m=0,
            error_rate_30m=0,
        )
        for feature in features.values()
    }
    history = HistorySnapshot(NOW, profile_stats)
    categories = {
        "model_key_hash": tuple(sorted(key.digest() for key in keys.values())),
        "provider_kind": ("offline",),
        "protocol": ("native",),
        "task_type": ("classification",),
    }
    numeric_names = (
        "streaming", "has_tools", "has_schema", "n", "log_input_proxy", "log_output_budget",
        "output_budget_missing", "reasoning", "reasoning_missing", "risk", "risk_missing",
        "hour_sin", "hour_cos", "weekday_sin", "weekday_cos", "log_inter_arrival_ms",
        "inter_arrival_missing", "log_measurement_age_ms", "history_missing", "log_ewma_ms",
        "log_median_ms", "log_p90_ms", "log_std_ms", "error_rate_30m", "timeout_rate_30m",
        "log_success_count_30m", "log_local_inflight", "log_local_rpm",
    )
    feature_names = tuple(
        [f"cat:{name}={value}" for name, values in categories.items() for value in values]
        + [f"num:{name}" for name in numeric_names]
    )
    coefficients = [0.0] * len(feature_names)
    for index, name in enumerate(feature_names):
        if name == f"cat:model_key_hash={keys[cheap_id].digest()}":
            coefficients[index] = math.log1p(900) - math.log1p(500)
        elif name == f"cat:model_key_hash={keys[quality_id].digest()}":
            coefficients[index] = math.log1p(50) - math.log1p(500)
    support = {
        feature.profile_key: {
            "model_key_hash": feature.model_key.digest(),
            "train_count": 60,
            "validation_count": 25,
            "input_proxy_p05": 0,
            "input_proxy_p95": 1000,
            "output_budget_p05": 100,
            "output_budget_p95": 5000,
            "train_missing_budget": 0,
            "validation_missing_budget": 0,
        }
        for feature in features.values()
    }
    profile_metrics = {profile: {"gate_pass": True, "validation_count": 25} for profile in support}
    artifact = LatencyArtifact(
        "latency-v1",
        "latency-v1-routing-test",
        feature_names,
        tuple(coefficients),
        math.log1p(500),
        {name: 0.0 for name in numeric_names},
        {name: 1.0 for name in numeric_names},
        categories,
        support,
        {"accepted": True, "profile_metrics": profile_metrics},
    )
    context = LatencyRequestContext(
        "request-routing",
        "api",
        "active",
        artifact,
        history,
        None,
        keys,
        application_id="application",
        resolver=LatencyResolver(artifact, history, LatencyConfig(mode="auto")),
    )
    candidates = [_candidate(cheap_id, 100, price=0.1), _candidate(quality_id, 800, price=15.0)]
    return context, features, candidates, cheap_id, quality_id


@pytest.mark.asyncio
async def test_auto_prediction_changes_route_while_off_uses_catalog_route():
    context, features, candidates, cheap_id, quality_id = _routing_state()
    config = SimpleNamespace(
        model_pool_id=None,
        routing_policy_id=None,
        fallback_enabled=True,
        fallback_model_id=None,
        fallback_model_ids_json=[],
    )
    rows = [(None, candidate.model, candidate.provider) for candidate in candidates]
    snapshot = DatabaseRoutingSnapshot(config, None, tuple(rows), RoutingSet(rows))
    service = DatabaseRoutingService(None, "application", snapshot=snapshot)
    constraints = RequestConstraints(quality_priority=QualityPriority.LATENCY_FIRST)

    with latency_request_scope(context):
        auto = await service.route(_task(), constraints, latency_features=features)
        assert auto.selected_model == quality_id

        context.mode = "off"
        context.resolver = LatencyResolver(context.artifact, context.history, LatencyConfig(mode="off"))
        catalog = await service.route(_task(), constraints, latency_features=features)

    assert catalog.selected_model == cheap_id
    assert context.resolutions_by_model[quality_id].gate_reason == "disabled"
