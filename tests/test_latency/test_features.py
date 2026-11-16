from datetime import UTC, datetime

from src.backend_ai.gateway.providers.base import GenerationOptions
from src.backend_ai.latency.contracts import HistorySnapshot, ModelKey
from src.backend_ai.latency.features import build_features
from src.shared.dtos.gateway import RoutingSignals


def test_feature_snapshot_contains_only_pre_execution_values():
    features = build_features(
        ModelKey("model-1", "provider-1", "upstream/model", "fingerprint"),
        material_chars=3000,
        options=GenerationOptions(max_output_tokens=512, n=2),
        task_type="coding",
        signals=RoutingSignals(reasoning=1.0, domain=1.0, ambiguity=0.0, risk=0.0, output=1.0),
        now=datetime(2026, 10, 14, 1, tzinfo=UTC),
        history=HistorySnapshot(datetime(2026, 10, 14, 1, tzinfo=UTC), {}),
        local_load={},
        routing_objective="latency_first",
    )

    assert features.values["input_proxy"] == 1000
    assert features.values["output_budget"] == 512
    assert features.values["n"] == 2
    assert features.values["routing_objective"] == "latency_first"
    assert "actual_output_tokens" not in features.values
    assert "cache_hit" not in features.values
    assert not any("prompt" in name for name in features.values)


def test_unknown_output_budget_is_marked_and_request_modes_have_distinct_profiles():
    key = ModelKey("model-1", "provider-1", "upstream/model", "fingerprint")
    now = datetime(2026, 10, 14, tzinfo=UTC)
    history = HistorySnapshot(now, {})
    plain = build_features(
        key, material_chars=0, options=GenerationOptions(), task_type=None, signals=None,
        now=now, history=history, local_load={},
    )
    stream = build_features(
        key, material_chars=0, options=GenerationOptions(tools=({},)), task_type=None, signals=None,
        now=now, history=history, local_load={}, protocol="openai_chat", streaming=True,
    )

    assert plain.values["output_budget_missing"] == 1
    assert plain.profile_key != stream.profile_key
    assert stream.values["streaming"] == 1
    assert stream.values["has_tools"] == 1
