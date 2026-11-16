from datetime import UTC, datetime

from pydantic import ValidationError

from src.backend_ai.latency.contracts import (
    LatencyConfig,
    LatencyFeatures,
    LatencyObservation,
    LatencyResolution,
    ModelKey,
    connection_fingerprint,
)
from src.shared.config import Settings


def test_connection_rotation_changes_key_without_exposing_credentials():
    first = connection_fingerprint(
        "openai", "https://user:secret@example.test/v1?token=hidden#fragment", 3, "ap-southeast-1"
    )
    same_connection = connection_fingerprint("openai", "https://example.test/v1", 3, "ap-southeast-1")
    rotated = connection_fingerprint("openai", "https://example.test/v1", 4, "ap-southeast-1")
    key = ModelKey("model-1", "provider-1", "upstream/model", first)

    assert first == same_connection
    assert first != rotated
    assert key.digest() != ModelKey("model-1", "provider-1", "upstream/model", rotated).digest()
    assert "secret" not in first and "hidden" not in first


def test_probe_update_keeps_model_key_stable():
    before_probe = ModelKey("model-1", "provider-1", "upstream/model", "fingerprint")
    after_probe = ModelKey("model-1", "provider-1", "upstream/model", "fingerprint")

    assert before_probe.digest() == after_probe.digest()


def test_latency_contracts_freeze_mapping_inputs():
    values = {"input_proxy": 1000}
    features = LatencyFeatures(
        model_key=ModelKey("model-1", "provider-1", "upstream/model", "fingerprint"),
        profile_key="profile",
        observed_at=datetime(2026, 10, 14, tzinfo=UTC),
        values=values,
    )
    values["input_proxy"] = 0

    assert features.values["input_proxy"] == 1000
    assert isinstance(features.values["input_proxy"], int)
    try:
        features.values["input_proxy"] = 0
    except TypeError:
        pass
    else:
        raise AssertionError("feature values must be immutable")


def test_latency_settings_default_auto_and_reject_old_modes_and_bad_limits():
    settings = Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:")
    config = LatencyConfig.from_settings(settings)

    assert settings.gateway_latency_mode == "auto"
    assert config.mode == "auto"
    assert config.min_common_validation_samples == 100
    assert config.training_enabled is True
    small_validation_settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        gateway_latency_min_common_validation_samples=2,
    )
    assert LatencyConfig.from_settings(small_validation_settings).min_common_validation_samples == 2
    try:
        Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", gateway_latency_mode="invalid")
    except ValidationError:
        pass
    else:
        raise AssertionError("invalid mode must be rejected")

    for old_mode in ("observe", "shadow", "active"):
        try:
            Settings(
                _env_file=None,
                database_url="sqlite+aiosqlite:///:memory:",
                gateway_latency_mode=old_mode,
            )
        except ValidationError as exc:
            message = str(exc)
            assert "legacy latency modes were removed" in message
            assert "GATEWAY_LATENCY_MODE=auto" in message
            assert "off to disable" in message
            continue
        raise AssertionError(f"legacy mode {old_mode!r} must be rejected")


def test_observation_and_resolution_keep_explicit_unknown_state():
    key = ModelKey("model-1", "provider-1", "upstream/model", "fingerprint")
    feature = LatencyFeatures(key, "profile", datetime(2026, 10, 14, tzinfo=UTC), {})
    observation = LatencyObservation(
        attempt_id="attempt-1",
        request_id="request-1",
        task_id="task-1",
        application_id="application-1",
        features=feature,
        source="playground",
        mode="observe",
        dispatched_at=datetime(2026, 10, 14, tzinfo=UTC),
        status="success",
        provider_total_ms=12.0,
    )
    result = LatencyResolution(None, None, "unknown", "unseen_model", None, {})

    assert observation.provider_total_ms == 12.0
    assert result.effective_ms is None
