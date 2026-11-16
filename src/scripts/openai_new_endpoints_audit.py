#!/usr/bin/env python3
"""Run a conservative OpenAI Python SDK smoke audit against a configured gateway.

Credentials are read only from an environment variable. The report contains
request/response IDs and assertions, never request bodies, vectors, or errors
that might echo provider data.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import openai
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field


class AuditCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: Literal["pass", "fail", "skipped"]
    evidence_kind: Literal[
        "unit_fixture", "tcp_controlled_provider", "live_provider", "live_gateway", "prerequisite"
    ]
    request_id: str | None = None
    assertions: list[str] = Field(default_factory=list)
    note: str | None = None


class AuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    started_at: datetime
    finished_at: datetime
    source_version: str
    sdk_version: str
    environment: dict[str, str]
    cases: list[AuditCase]
    missing_prerequisites: list[str] = Field(default_factory=list)
    cleanup_errors: list[str] = Field(default_factory=list)


def exit_code(report: AuditReport) -> int:
    if report.cleanup_errors or any(case.status == "fail" for case in report.cases):
        return 1
    if report.missing_prerequisites:
        return 2
    if any(case.status == "skipped" for case in report.cases):
        return 3
    return 0


def safe_url(value: str) -> str:
    """Drop user info, query and fragment from a potentially credentialed URL."""
    try:
        parts = urlsplit(value)
        hostname = parts.hostname or ""
        if parts.port:
            hostname = f"{hostname}:{parts.port}"
        return urlunsplit((parts.scheme, hostname, parts.path, "", ""))
    except ValueError:
        return "<invalid-url>"


def missing_catalog_prerequisites(visible_ids: set[str], response_model: str, embedding_model: str) -> list[str]:
    required = {response_model, embedding_model}
    return [f"model {model_id!r} is not visible to this application" for model_id in sorted(required - visible_ids)]


def source_version() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=2
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def worktree_state() -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True, timeout=2
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return "dirty" if result.stdout.strip() else "clean"


def _case(case_id: str, status: str, assertions: list[str], *, request_id: str | None = None,
          note: str | None = None, evidence_kind: str = "live_gateway") -> AuditCase:
    return AuditCase(
        id=case_id,
        status=status,
        evidence_kind=evidence_kind,
        request_id=request_id,
        assertions=assertions,
        note=note,
    )


def run_audit(
    *,
    base_url: str | None,
    api_key_env: str,
    response_model: str,
    embedding_model: str,
) -> tuple[AuditReport, int]:
    started = datetime.now(UTC)
    key = os.environ.get(api_key_env, "")
    missing: list[str] = []
    if not base_url:
        missing.append("--base-url is required")
    if not key:
        missing.append(f"environment variable {api_key_env} is missing")
    report = AuditReport(
        run_id=str(uuid4()),
        started_at=started,
        finished_at=started,
        source_version=source_version(),
        sdk_version=openai.__version__,
        environment={
            "gateway_base_url": safe_url(base_url) if base_url else "missing",
            "working_tree": worktree_state(),
        },
        cases=[],
        missing_prerequisites=missing,
    )
    if missing:
        report.cases.append(
            _case("live_prerequisites", "skipped", [], note="; ".join(missing), evidence_kind="prerequisite")
        )
        report.finished_at = datetime.now(UTC)
        return report, 2

    client = OpenAI(base_url=base_url, api_key=key, max_retries=0)
    response_key = f"openai-audit-response-{uuid4().hex}"
    embedding_key = f"openai-audit-embedding-{uuid4().hex}"
    try:
        try:
            models = client.models.list()
            visible_ids = {model.id for model in models.data}
            report.cases.append(
                _case("models_list", "pass", [f"visible cards={len(visible_ids)}"],
                      request_id=models._request_id)
            )
        except Exception as exc:
            report.cases.append(_safe_error_case("models_list", exc))
            visible_ids = set()

        for model_id in dict.fromkeys((response_model, embedding_model)):
            if model_id not in visible_ids:
                report.cases.append(
                    _case(f"model_retrieve:{model_id}", "skipped", [],
                          note="requested model is not in the visible catalog", evidence_kind="prerequisite")
                )
                continue
            try:
                card = client.models.retrieve(model_id)
                report.cases.append(
                    _case(f"model_retrieve:{model_id}", "pass", ["card id matches requested model"],
                          request_id=card._request_id if hasattr(card, "_request_id") else None)
                )
                if card.id != model_id:
                    report.cases[-1] = _case(f"model_retrieve:{model_id}", "fail", ["card id mismatch"])
            except Exception as exc:
                report.cases.append(_safe_error_case(f"model_retrieve:{model_id}", exc))

        if response_model in visible_ids:
            report.cases.extend(_response_checks(client, response_model, response_key))

        if embedding_model in visible_ids:
            try:
                vector = client.embeddings.create(
                    model=embedding_model,
                    input="gateway embedding audit",
                    extra_headers={"Idempotency-Key": embedding_key},
                )
                vector_replay = client.embeddings.create(
                    model=embedding_model,
                    input="gateway embedding audit",
                    extra_headers={"Idempotency-Key": embedding_key},
                )
                stable = (
                    vector.data == vector_replay.data
                    and vector.usage == vector_replay.usage
                    and vector.model == vector_replay.model
                )
                report.cases.append(
                    _case(
                        "embeddings_success_replay",
                        "pass" if stable else "fail",
                        ["SDK decoded default encoding", "same vector/usage/model on replay"]
                        if stable else ["embedding replay changed result"],
                        request_id=vector_replay._request_id,
                        evidence_kind="live_provider",
                    )
                )
            except Exception as exc:
                report.cases.append(_safe_error_case("embeddings_success_replay", exc))
        missing_catalog = missing_catalog_prerequisites(visible_ids, response_model, embedding_model)
        if missing_catalog:
            report.missing_prerequisites.extend(missing_catalog)
        # These require a separate application/key, catalog mutation, or
        # isolated fault-injection deployment; a user application key cannot
        # safely establish those oracles on a live gateway.
        for case_id, note in (
            ("cross_surface_conflict", "requires receipt oracle and shared-key fault case"),
            ("catalog_change_replay", "requires a controlled catalog mutation"),
            ("foreign_or_revoked_key", "requires a second application and key lifecycle access"),
            ("provider_failure_replay", "requires an isolated controlled provider fault"),
            ("receipt_usage_and_billing_oracle", "requires management receipt and billing read access"),
        ):
            report.cases.append(_case(case_id, "skipped", [], note=note))
    finally:
        try:
            client.close()
        except Exception as exc:
            report.cleanup_errors.append(f"client close failed ({type(exc).__name__})")
        report.finished_at = datetime.now(UTC)
    return report, exit_code(report)


def _safe_error_case(case_id: str, exc: Exception) -> AuditCase:
    status = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)
    description = f"SDK request raised {type(exc).__name__}"
    if isinstance(status, int):
        description += f" (HTTP {status})"
    return _case(case_id, "fail", [description], request_id=request_id)


def _stream_request_id(stream, final_response=None) -> str | None:
    request_id = getattr(final_response, "_request_id", None)
    if request_id:
        return request_id
    http_response = getattr(stream, "_response", None)
    headers = getattr(http_response, "headers", None)
    if headers is not None:
        return headers.get("x-request-id") or headers.get("X-Request-ID")
    return None


def _response_sse_check(client: OpenAI, model: str, idempotency_key: str) -> AuditCase:
    try:
        with client.responses.stream(
            model=model,
            input="Trả lời ngắn gọn: OK.",
            store=False,
            extra_headers={"Idempotency-Key": idempotency_key},
        ) as stream:
            request_id = _stream_request_id(stream)
            events = list(stream)
            final = stream.get_final_response()
            request_id = _stream_request_id(stream, final) or request_id
        terminal = events[-1].type if events else "missing"
        valid = terminal in {"response.completed", "response.incomplete"} and bool(final.output_text)
        return _case(
            "responses_typed_sse",
            "pass" if valid else "fail",
            [f"terminal event={terminal}", "SDK reconstructed final response"],
            request_id=request_id,
        )
    except Exception as exc:
        return _safe_error_case("responses_typed_sse", exc)


def _response_checks(client: OpenAI, model: str, idempotency_key: str) -> list[AuditCase]:
    cases: list[AuditCase] = []
    try:
        first = client.responses.create(
            model=model,
            input="Trả lời ngắn gọn: OK.",
            store=False,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        replay = client.responses.create(
            model=model,
            input="Trả lời ngắn gọn: OK.",
            store=False,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
    except Exception as exc:
        return [_safe_error_case("responses_success_replay", exc)]
    stable = first.id == replay.id and first.output_text == replay.output_text
    cases.append(
        _case(
            "responses_success_replay",
            "pass" if stable else "fail",
            ["same response id", "same output text"] if stable else ["response replay changed id or text"],
            request_id=replay._request_id,
        )
    )
    try:
        changed = client.responses.create(
            model=model,
            input="Nội dung đã đổi.",
            store=False,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
    except openai.ConflictError as exc:
        cases.append(
            _case("responses_changed_body_conflict", "pass", ["same key with changed input returns 409"],
                  request_id=getattr(exc, "request_id", None))
        )
    except Exception as exc:
        cases.append(_safe_error_case("responses_changed_body_conflict", exc))
    else:
        cases.append(_case("responses_changed_body_conflict", "fail", ["expected HTTP 409"],
                           request_id=changed._request_id))
    cases.append(_response_sse_check(client, model, f"{idempotency_key}-stream"))
    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Gateway OpenAI base URL ending in /v1")
    parser.add_argument("--api-key-env", default="GATEWAY_API_KEY", help="Environment variable containing application key")
    parser.add_argument("--response-model", default="auto")
    parser.add_argument("--embedding-model", default="text-embedding-3-small")
    parser.add_argument("--output", type=Path, help="Write the JSON evidence report to this path")
    args = parser.parse_args(argv)
    report, code = run_audit(
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        response_model=args.response_model,
        embedding_model=args.embedding_model,
    )
    output = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
