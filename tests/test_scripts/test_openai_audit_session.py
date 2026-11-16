import json

import httpx
import pytest

from src.scripts.openai_audit_session import AuditPrerequisiteError, AuditSession


def test_login_resolves_owned_apps_and_redacts_reports():
    def handle(request):
        if request.url.path.endswith("/token"):
            assert request.read() == b'{"email":"account@example.test","password":"secret-password"}'
            return httpx.Response(200, json={"access_token": "secret-token"})
        if request.url.path.endswith("/applications"):
            return httpx.Response(200, json={"items": [{"id": "owned"}], "total": 1})
        if request.url.path.endswith("/api-keys"):
            return httpx.Response(200, json={"items": [{"key_prefix": "ak_123456789", "revoked": False}], "total": 1})
        return httpx.Response(200, json={})

    with AuditSession(
        "https://auth.test", "publishable", "https://mgmt.test/api/v1", transport=httpx.MockTransport(handle)
    ) as session:
        session.login("account@example.test", "secret-password")
        assert session.resolve_application("app_1", "ak_1234567890more") == "owned"
        result = session.audit_case("C07", "fixture", {"ownership": True})
        serialized = json.dumps(result)
        for secret in ("secret-token", "secret-password", "account@example.test", "ak_1234567890more"):
            assert secret not in serialized


def test_unowned_or_ambiguous_key_cannot_resolve_application():
    def handle(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "private"})
        return httpx.Response(200, json={"items": [], "total": 0})

    with AuditSession(
        "https://auth.test", "publishable", "https://mgmt.test/api/v1", transport=httpx.MockTransport(handle)
    ) as session:
        session.login("private", "private")
        with pytest.raises(AuditPrerequisiteError):
            session.resolve_application("app_1", "private-key")
        assert session.audit_case("C07", "fixture", {}, prerequisite=False)["status"] == "skipped"


def test_report_rejects_arbitrary_secret_fields():
    with AuditSession("https://auth.test", "publishable", "https://mgmt.test/api/v1") as session:
        with pytest.raises(ValueError):
            session.audit_case("C07", "fixture", {"Authorization": "secret"})


def test_cleanup_runs_after_mid_audit_failure_and_continues_after_cleanup_failure():
    cleanup = []
    audit = AuditSession('https://auth.test', 'publishable', 'https://mgmt.test')
    def failing_cleanup():
        cleanup.append('broken')
        raise RuntimeError('private failure')
    with pytest.raises(ValueError):
        with audit:
            audit.register_cleanup('safe', lambda: cleanup.append('safe'))
            audit.register_cleanup('broken', failing_cleanup)
            raise ValueError('private audit failure')
    assert cleanup == ['broken', 'safe']
    assert audit.cleanup_results == [{'resource': 'broken', 'status': 'fail'}, {'resource': 'safe', 'status': 'pass'}]
    assert audit.access_token is None


def test_expected_application_mismatch_is_a_prerequisite_failure():
    def handle(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200, json={'access_token': 'private'})
        if request.url.path.endswith('/applications'):
            return httpx.Response(200, json={'items': [{'id': 'owned'}], 'total': 1})
        return httpx.Response(200, json={'items': [{'key_prefix': 'ak_123456789', 'revoked': False}], 'total': 1})
    with AuditSession('https://auth.test', 'publishable', 'https://mgmt.test', transport=httpx.MockTransport(handle)) as audit:
        audit.login('private', 'private')
        with pytest.raises(AuditPrerequisiteError):
            audit.resolve_application('app_1', 'ak_123456789more', expected_id='foreign')
        assert not audit.applications
