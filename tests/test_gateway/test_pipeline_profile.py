"""Online vs offline pipeline: one switch (GATEWAY_EXECUTION_MODE) decides every stage's configuration."""

import itertools

import pytest
from pydantic import ValidationError

from src.backend_ai.gateway import factory, pipeline_profile
from src.backend_ai.gateway.pipeline_profile import OFFLINE, ONLINE, STAGES, configuration_issues, log_configuration
from src.backend_ai.gateway.understanding.task_classifier import RemoteTaskClassifier
from src.backend_ai.services.internal_provider_clients import build_internal_clients
from src.shared.config import Settings

URL = "https://clf.example"


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", **overrides)


@pytest.mark.parametrize("value, expected", [("online", "live"), ("ONLINE", "live"), ("live", "live"), (" Offline ", "offline")])
def test_the_pipeline_switch_accepts_online_as_live(value, expected):
    assert settings(gateway_execution_mode=value).gateway_execution_mode == expected


def test_an_unknown_pipeline_is_rejected():
    with pytest.raises(ValidationError):
        settings(gateway_execution_mode="hybrid")


def test_the_switch_selects_the_profile():
    assert pipeline_profile.pipeline_profile(settings(gateway_execution_mode="online")) is ONLINE
    assert pipeline_profile.pipeline_profile(settings(gateway_execution_mode="offline")) is OFFLINE
    assert ONLINE.external_services and not OFFLINE.external_services


def test_online_keeps_every_configured_strategy():
    for combination in itertools.product(*(sorted(ONLINE.allowed[stage]) for stage in STAGES)):
        requested = dict(zip(STAGES, combination, strict=True))
        assert ONLINE.resolve(requested) == (requested, {})


def test_offline_runs_only_local_stages_whatever_the_application_configured():
    for combination in itertools.product(*(sorted(ONLINE.allowed[stage]) for stage in STAGES)):
        requested = dict(zip(STAGES, combination, strict=True))
        effective, substituted = OFFLINE.resolve(requested)
        for stage in STAGES:
            assert effective[stage] in OFFLINE.allowed[stage], (stage, requested)
            assert "llm" != effective[stage] and "jev" != effective[stage]
        assert set(substituted) == {stage for stage in STAGES if effective[stage] != requested[stage]}


def test_offline_substitutions_are_reported():
    effective, substituted = OFFLINE.resolve({"understander": "jev", "decomposer": "llm", "aggregator": "auto"})
    assert effective == {"understander": "classifier", "decomposer": "deterministic", "aggregator": "auto"}
    assert substituted == {
        "understander": {"requested": "jev", "effective": "classifier"},
        "decomposer": {"requested": "llm", "effective": "deterministic"},
    }


class ResolverThatMustNotBeCalled:
    async def resolve_service(self, purpose):
        raise AssertionError(f"offline resolved the {purpose} credential")


@pytest.mark.asyncio
async def test_offline_never_resolves_llm_jev_or_ocr_credentials():
    clients = await build_internal_clients(
        ResolverThatMustNotBeCalled(),
        settings(gateway_execution_mode="offline", gateway_openai_enabled=True, gateway_jev_enabled=True,
                 gateway_ocr_enabled=True),
        need_llm=True, need_jev=True, need_ocr=True, need_legacy_router=True,
    )
    assert clients.llm_client is None and clients.jev_client is None and clients.ocr_client is None


def test_both_pipelines_use_the_classifier_service_when_a_url_is_set(tmp_path):
    factory._remote_task_classifier.cache_clear()
    factory._load_task_classifier.cache_clear()
    for mode in ("online", "offline"):
        configured = settings(gateway_execution_mode=mode, gateway_task_classifier_url=URL)
        assert isinstance(factory.build_task_classifier(configured), RemoteTaskClassifier), mode
        assert factory.task_classifier_status(configured)["mode"] == "remote", mode
    # No URL: the local model (or the rules when there is none), never a service.
    local = settings(gateway_execution_mode="offline", gateway_task_classifier_dir=str(tmp_path))
    assert factory.task_classifier_status(local)["mode"] == "local"
    assert factory.build_task_classifier(settings(gateway_execution_mode="offline")) is None


def test_offline_configuration_issues():
    assert configuration_issues(settings(gateway_execution_mode="offline", gateway_task_classifier_dir="/m")) == []

    # A classifier service is allowed offline; the report says the focus text is sent there.
    [(level, message)] = configuration_issues(settings(gateway_execution_mode="offline", gateway_task_classifier_url=URL))
    assert level == "info" and "focus text is sent" in message

    # No classifier at all: the rules serve, with a warning.
    [(level, message)] = configuration_issues(settings(gateway_execution_mode="offline"))
    assert level == "warning" and "GATEWAY_TASK_CLASSIFIER_URL nor GATEWAY_TASK_CLASSIFIER_DIR" in message

    ignored = configuration_issues(settings(gateway_execution_mode="offline", gateway_task_classifier_dir="/m",
                                            gateway_jev_enabled=True))
    assert [level for level, _ in ignored] == ["info"] and "GATEWAY_JEV_ENABLED" in ignored[0][1]


def test_a_required_classifier_with_no_source_stops_the_offline_pipeline():
    required = settings(gateway_execution_mode="offline", gateway_task_classifier_required=True)
    assert {level for level, _ in configuration_issues(required)} == {"error"}
    with pytest.raises(RuntimeError, match="invalid offline pipeline configuration"):
        log_configuration(required)
    # A service URL is a valid source in either pipeline.
    for mode in ("online", "offline"):
        configured = settings(gateway_execution_mode=mode, gateway_task_classifier_required=True,
                              gateway_task_classifier_url=URL, gateway_openai_enabled=True, gateway_jev_enabled=True)
        assert all(level != "error" for level, _ in log_configuration(configured))


def test_online_configuration_issues_are_informational():
    issues = configuration_issues(settings(gateway_execution_mode="online", gateway_openai_enabled=False,
                                           gateway_jev_enabled=False))
    assert [level for level, _ in issues] == ["info", "info"]


def _app(pipeline):
    from types import SimpleNamespace

    return SimpleNamespace(pipeline=pipeline)


def _config(mid=None, large=None):
    from types import SimpleNamespace

    return SimpleNamespace(classifier_mid_difficulty=mid, classifier_large_difficulty=large)


def test_each_request_gets_its_own_pipeline_on_an_online_server():
    from src.backend_ai.gateway.pipeline_profile import settings_for_application

    server = settings(gateway_execution_mode="online")
    online = settings_for_application(server, _app("online"), _config())
    offline = settings_for_application(server, _app("offline"), _config(0.25, 0.55))

    assert pipeline_profile.pipeline_profile(online) is ONLINE
    assert pipeline_profile.pipeline_profile(offline) is OFFLINE
    assert (offline.gateway_classifier_mid_difficulty, offline.gateway_classifier_large_difficulty) == (0.25, 0.55)
    assert online is server  # nothing to change: the same object
    assert server.gateway_execution_mode == "live"  # the process settings are never changed


def test_an_offline_server_keeps_every_request_offline():
    from src.backend_ai.gateway.pipeline_profile import settings_for_application

    server = settings(gateway_execution_mode="offline")
    assert settings_for_application(server, _app("online"), None).gateway_execution_mode == "offline"


def test_per_request_settings_drive_the_stages():
    """The offline copy really selects the offline stages: no llm stages, only the local providers."""
    from src.backend_ai.gateway.pipeline_profile import settings_for_application

    server = settings(gateway_execution_mode="online", gateway_openai_enabled=True)
    offline = settings_for_application(server, _app("offline"), _config(0.3, 0.8))
    assert factory.build_tier_policy(offline).config.classifier_thresholds == (0.3, 0.8)
    assert factory.build_tier_policy(server).config.classifier_thresholds == (0.23, 0.61)
    assert set(factory.build_providers(offline)) == {"offline"}
    assert factory.build_providers(server) == {}


class _KeyRepository:
    def __init__(self, raw_key, pipeline):
        import hashlib
        from types import SimpleNamespace

        self.record = SimpleNamespace(id="k1", application_id="a1", revoked_at=None,
                                      key_hash=hashlib.sha256(raw_key.encode()).hexdigest())
        self.application = SimpleNamespace(id="a1", status="active", pipeline=pipeline)

    async def get_by_prefix(self, prefix):
        return self.record

    async def get_application(self, application_id):
        return self.application

    async def touch_last_used(self, key_id):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw_key", "pipeline", "accepted"), [
    ("off_" + "a" * 43, "offline", True),
    ("ak_" + "a" * 43, "online", True),
    ("ak_" + "a" * 43, "offline", False),   # an offline pipeline only answers to off_ keys
    ("off_" + "a" * 43, "online", False),
    ("xx_" + "a" * 43, "online", False),
])
async def test_the_key_prefix_must_match_the_pipeline(raw_key, pipeline, accepted):
    from src.backend_ai.services.application_auth_service import ApplicationKeyAuthService, InvalidApplicationKeyError

    service = ApplicationKeyAuthService.__new__(ApplicationKeyAuthService)
    service.repository = _KeyRepository(raw_key, pipeline)
    if accepted:
        principal = await service.authenticate(raw_key)
        assert (principal.application_id, principal.pipeline) == ("a1", pipeline)
    else:
        with pytest.raises(InvalidApplicationKeyError):
            await service.authenticate(raw_key)


def test_health_reports_the_pipeline(monkeypatch):
    from src.backend_ai import main

    monkeypatch.setattr(main, "get_settings", lambda: settings(gateway_execution_mode="offline"))
    monkeypatch.setattr(main, "task_classifier_status",
                        lambda: {"configured": False, "mode": None, "loaded": False, "model": None, "error": None})
    details = main.ai_health_details()
    assert details["pipeline"] == "offline"
    assert details["pipeline_issues"][0]["level"] == "warning"
    assert "status" not in details  # the rules still serve it: listed, not degraded
