import json
from datetime import UTC, datetime

import httpx

from src.scripts import openai_sdk_contract_audit as audit


def _config(*, skip_generation=True):
    return audit.AuditConfig(
        base_url="http://gateway.test/v1",
        api_key="sk_test_do_not_emit",
        model="auto",
        skip_generation=skip_generation,
    )


def _file_object(file_id):
    now = int(datetime.now(UTC).timestamp())
    return {
        "id": file_id,
        "object": "file",
        "bytes": 4,
        "created_at": now,
        "expires_at": now + 3600,
        "filename": f"{file_id}.txt",
        "purpose": "user_data",
        "status": "processed",
        "status_details": None,
    }


def test_cleanup_runs_after_mid_flow_failure_and_output_redacts_credentials():
    secret = "sk_test_do_not_emit"
    created = []
    deleted = []

    def handler(request):
        if not request.headers.get("authorization"):
            return httpx.Response(401, json={"error": {"message": "unauthorized", "code": "invalid_api_key"}})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "auto", "object": "model", "created": 0, "owned_by": "test"}]},
            )
        if request.method == "POST" and request.url.path == "/v1/files":
            file_id = f"file-test-{len(created)}"
            created.append(file_id)
            return httpx.Response(200, json=_file_object(file_id))
        if request.method == "GET" and request.url.path == "/v1/files":
            raise RuntimeError(f"failure for {secret}; Authorization: Bearer {secret}")
        if request.method == "GET" and request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"data", headers={"content-type": "text/plain"})
        if request.method == "GET" and request.url.path.startswith("/v1/files/"):
            return httpx.Response(200, json=_file_object(request.url.path.rsplit("/", 1)[-1]))
        if request.method == "DELETE" and request.url.path.startswith("/v1/files/"):
            file_id = request.url.path.rsplit("/", 1)[-1]
            deleted.append(file_id)
            return httpx.Response(200, json={"id": file_id, "object": "file", "deleted": True})
        return httpx.Response(404, json={"error": {"message": "not found", "code": "not_found"}})

    report = audit.run_audit(_config(), transport=httpx.MockTransport(handler))
    serialized = json.dumps(report)

    assert len(created) == 3
    assert set(deleted) == set(created)
    assert report["checks"]["B16"]["status"] == "fail"
    assert secret not in serialized
    assert "Authorization" not in serialized
    assert "Bearer " not in serialized


def test_skip_generation_is_reported_as_partial_not_full_pass():
    uploaded = []

    def handler(request):
        if not request.headers.get("authorization"):
            return httpx.Response(401, json={"error": {"message": "unauthorized", "code": "invalid_api_key"}})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "auto", "object": "model", "created": 0, "owned_by": "test"}]},
            )
        if request.method == "POST" and request.url.path == "/v1/files":
            file_id = f"file-test-{len(uploaded)}"
            uploaded.append(file_id)
            return httpx.Response(200, json=_file_object(file_id))
        if request.method == "GET" and request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"data", headers={"content-type": "text/plain"})
        if request.method == "GET" and request.url.path.startswith("/v1/files/"):
            return httpx.Response(200, json=_file_object(request.url.path.rsplit("/", 1)[-1]))
        if request.method == "GET" and request.url.path == "/v1/files":
            return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})
        if request.method == "DELETE" and request.url.path.startswith("/v1/files/"):
            file_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"id": file_id, "object": "file", "deleted": True})
        return httpx.Response(401, json={"error": {"message": "unauthorized", "code": "invalid_api_key"}})

    report = audit.run_audit(_config(), transport=httpx.MockTransport(handler))

    assert report["scope"] == "files_only"
    assert report["full_suite_passed"] is False
    assert report["checks"]["B02"]["status"] == "skipped"
    assert report["checks"]["B14"]["status"] == "skipped"
    assert report["checks"]["B18"]["status"] == "skipped"
    assert report["checks"]["B16"]["status"] != "pass"


def test_cli_returns_partial_exit_code_when_report_has_skipped_checks(monkeypatch, capsys):
    monkeypatch.setenv("GATEWAY_OPENAI_BASE_URL", "http://gateway.test/v1")
    monkeypatch.setenv("GATEWAY_API_KEY", "sk_test_not_logged")
    monkeypatch.setenv("GATEWAY_OPENAI_MODEL", "auto")
    monkeypatch.setattr(
        audit,
        "run_audit",
        lambda config: {
            "scope": "full",
            "status": "partial",
            "full_suite_passed": False,
            "summary": {"pass": 27, "fail": 0, "skipped": 2},
        },
    )

    assert audit.main([]) == 3
    output = capsys.readouterr()
    assert '"status": "partial"' in output.out
    assert "contract audit: partial" in output.err


def test_missing_configuration_exits_before_creating_a_client(monkeypatch, capsys):
    for name in (
        "GATEWAY_OPENAI_BASE_URL",
        "GATEWAY_API_KEY",
        "GATEWAY_OPENAI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("missing configuration must be detected before network clients are created")

    monkeypatch.setattr(audit, "OpenAI", fail_if_constructed)
    monkeypatch.setattr(audit, "AsyncOpenAI", fail_if_constructed)

    assert audit.main([]) == 2
    output = capsys.readouterr()
    assert "Missing required configuration" in output.err
    assert "sk_test_do_not_emit" not in output.out + output.err
