from datetime import UTC, datetime
from types import SimpleNamespace

from src.scripts.openai_new_endpoints_audit import (
    AuditCase,
    AuditReport,
    _response_sse_check,
    _stream_request_id,
    exit_code,
    missing_catalog_prerequisites,
    run_audit,
    safe_url,
)


def _report(*, statuses=("pass",), prerequisites=(), cleanup=()):
    now = datetime.now(UTC)
    return AuditReport(
        run_id="run-1",
        started_at=now,
        finished_at=now,
        source_version="source",
        sdk_version="2.54.0",
        environment={"gateway_base_url": "http://localhost/v1"},
        cases=[AuditCase(id=str(index), status=status, evidence_kind="live_gateway")
               for index, status in enumerate(statuses)],
        missing_prerequisites=list(prerequisites),
        cleanup_errors=list(cleanup),
    )


def test_skipped_cases_are_partial_nonzero_and_failures_are_nonzero():
    assert exit_code(_report(statuses=("pass", "skipped"))) == 3
    assert exit_code(_report(statuses=("pass", "fail"))) == 1
    assert exit_code(_report(statuses=("pass",))) == 0


def test_cleanup_failure_fails_and_missing_prerequisite_has_distinct_exit_code():
    assert exit_code(_report(cleanup=("close failed",))) == 1
    assert exit_code(_report(prerequisites=("embedding model missing",))) == 2


def test_report_redacts_credentials_from_gateway_url():
    assert safe_url("https://user:password@gateway.example/v1?token=secret#frag") == (
        "https://gateway.example/v1"
    )
    report, code = run_audit(
        base_url="https://user:password@gateway.example/v1?token=secret",
        api_key_env="OPENAI_AUDIT_TEST_MISSING_KEY",
        response_model="auto",
        embedding_model="text-embedding-3-small",
    )
    assert code == 2
    serialized = report.model_dump_json()
    assert "password" not in serialized
    assert "secret" not in serialized
    assert "OPENAI_AUDIT_TEST_MISSING_KEY" in serialized


def test_missing_catalog_is_a_prerequisite_not_a_pass():
    missing = missing_catalog_prerequisites({"auto"}, "auto", "text-embedding-3-small")
    assert missing == ["model 'text-embedding-3-small' is not visible to this application"]


def test_stream_request_id_comes_from_http_headers_when_sdk_final_has_no_id():
    class FinalResponse:
        pass

    class StreamResponse:
        _response = type("HttpResponse", (), {"headers": {"x-request-id": "stream-request-1"}})()

    assert _stream_request_id(StreamResponse(), FinalResponse()) == "stream-request-1"


def test_sse_audit_uses_http_request_id_not_reconstructed_response_private_field():
    class FakeStream:
        _response = SimpleNamespace(headers={"x-request-id": "stream-request-2"})

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            return iter([SimpleNamespace(type="response.completed")])

        def get_final_response(self):
            return SimpleNamespace(output_text="OK")

    class FakeResponses:
        def stream(self, **kwargs):
            assert kwargs["store"] is False
            return FakeStream()

    case = _response_sse_check(SimpleNamespace(responses=FakeResponses()), "auto", "stable-key")

    assert case.status == "pass"
    assert case.request_id == "stream-request-2"
