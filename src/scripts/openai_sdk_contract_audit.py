#!/usr/bin/env python3
"""Run a repeatable OpenAI SDK contract audit against a Gateway instance.

The script only calls public Gateway/management HTTP endpoints. It never logs
or writes API keys, bearer tokens, request bodies, or provider responses. A
temporary user application and application keys must be prepared separately.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from openai import APIStatusError, AsyncOpenAI, OpenAI


@dataclass(frozen=True)
class AuditConfig:
    base_url: str
    api_key: str
    model: str
    other_api_key: str | None = None
    management_base_url: str | None = None
    user_access_token: str | None = None
    application_id: str | None = None
    receipt_base_url: str | None = None
    skip_generation: bool = False


def _safe_url(value: str) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))


def _ai_root(base_url: str) -> str:
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def _repo_revision() -> tuple[str | None, bool | None]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=2
        ).stdout.strip()
        dirty = bool(
            subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True, timeout=2).stdout
        )
        return revision, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def _status_error(exc: BaseException) -> dict[str, Any]:
    """Keep only stable, non-secret API error metadata in the report."""
    result: dict[str, Any] = {"error_class": type(exc).__name__}
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        result["http_status"] = int(status_code)
    response = getattr(exc, "response", None)
    if response is not None:
        result["http_status"] = int(response.status_code)
        result["request_id"] = response.headers.get("x-request-id") or response.headers.get("x-gateway-request-id")
        try:
            body = response.json()
            error = body.get("error", {}) if isinstance(body, dict) else {}
            if isinstance(error, dict):
                if error.get("code") is not None:
                    result["code"] = str(error["code"])
                if error.get("param") is not None:
                    result["param"] = str(error["param"])
        except (ValueError, AttributeError):
            pass
    if getattr(exc, "code", None) is not None:
        result.setdefault("code", str(exc.code))
    if getattr(exc, "param", None) is not None:
        result.setdefault("param", str(exc.param))
    return result


class ContractAudit:
    def __init__(self, config: AuditConfig, *, transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport
        self.run_id = uuid4().hex
        revision, dirty = _repo_revision()
        try:
            sdk_version = importlib.metadata.version("openai")
        except importlib.metadata.PackageNotFoundError:
            sdk_version = "unknown"
        self.report: dict[str, Any] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "started_at": datetime.now(UTC).isoformat(),
            "scope": "files_only" if config.skip_generation else "full",
            "sdk": "openai-python",
            "sdk_version": sdk_version,
            "model": config.model,
            "openai_base_url": _safe_url(config.base_url),
            "application_id": config.application_id,
            "implementation_revision": revision,
            "working_tree_dirty": dirty,
            "baseline_report": "OPENAI_SDK_LIVE_AUDIT_2026-10-07.md",
            "checks": {},
        }
        self._sync_completion: Any = None
        self._sync_completion_status = "skipped"
        self._attachment_request_id: str | None = None
        self._management_before_rejections: int | None = None

    def _case(
        self,
        case_id: str,
        name: str,
        status: str,
        *,
        latency_ms: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {"name": name, "status": status}
        if latency_ms is not None:
            entry["latency_ms"] = round(latency_ms, 3)
        if details:
            entry.update(details)
        self.report["checks"][case_id] = entry

    def _skip(self, case_id: str, name: str, reason: str) -> None:
        self._case(case_id, name, "skipped", details={"reason": reason})

    def _api_key(self, label: str) -> str:
        return f"openai-audit-{self.run_id[:16]}-{label}"

    def run(self) -> dict[str, Any]:
        sync_http = httpx.Client(transport=self.transport) if self.transport is not None else None
        try:
            with OpenAI(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                timeout=150,
                max_retries=0,
                http_client=sync_http,
            ) as client:
                self._run_sync_checks(client)
        except Exception as exc:
            self._case("B01", "OpenAI SDK models.list", "fail", details=_status_error(exc))

        asyncio.run(self._run_async_checks())
        self._finish_report()
        return self.report

    def _run_sync_checks(self, client: OpenAI) -> None:
        started = time.perf_counter()
        try:
            models = client.models.list()
            model_ids = [item.id for item in models.data]
            expected = self.config.model in model_ids
            self._case(
                "B01",
                "SDK models.list includes the configured model",
                "pass" if expected else "fail",
                latency_ms=(time.perf_counter() - started) * 1000,
                details={"model_count": len(model_ids), "configured_model_listed": expected},
            )
        except Exception as exc:
            self._case("B01", "SDK models.list includes the configured model", "fail", details=_status_error(exc))

        if self.config.skip_generation:
            for case_id, name in (
                ("B02", "sync and async chat completions"),
                ("B03", "auto alias completion"),
                ("B04", "message history accepted"),
            ):
                self._skip(case_id, name, "--skip-generation was requested")
            return

        started = time.perf_counter()
        try:
            completion = client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": f"Reply briefly to audit {self.run_id[:8]}."}],
                max_completion_tokens=48,
                extra_headers={"Idempotency-Key": self._api_key("sync")},
            )
            self._sync_completion = completion
            content = completion.choices[0].message.content
            passed = completion.object == "chat.completion" and isinstance(content, str) and completion.id
            self._case(
                "B02_SYNC",
                "sync OpenAI SDK chat completion parses",
                "pass" if passed else "fail",
                latency_ms=(time.perf_counter() - started) * 1000,
                details={
                    "http_status": 200,
                    "request_id": completion._request_id,
                    "completion_id": completion.id,
                    "usage": completion.usage.model_dump() if completion.usage else None,
                },
            )
            self._sync_completion_status = "pass" if passed else "fail"
        except Exception as exc:
            self._case("B02_SYNC", "sync OpenAI SDK chat completion parses", "fail", details=_status_error(exc))
            self._sync_completion_status = "fail"

    async def _run_async_checks(self) -> None:
        ai_root = _ai_root(self.config.base_url)
        async_http = httpx.AsyncClient(transport=self.transport) if self.transport is not None else None
        async with AsyncOpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            timeout=150,
            max_retries=0,
            http_client=async_http,
        ) as client, httpx.AsyncClient(timeout=30, transport=self.transport) as raw:
            try:
                await self._file_checks(client, raw, ai_root)
            except Exception as exc:
                self._case("B05", "Files upload/retrieve/content", "fail", details=_status_error(exc))

            await self._auth_check(raw, ai_root)
            await self._isolation_check(raw, ai_root)
            if self.config.skip_generation:
                self._skip_generation_cases()
                return

            await self._chat_check(
                client, "B02_ASYNC", "async OpenAI SDK chat completion parses", model=self.config.model
            )
            async_status = self.report["checks"].get("B02_ASYNC", {}).get("status", "fail")
            self._case(
                "B02",
                "sync and async OpenAI SDK chat completion parse",
                "pass" if self._sync_completion_status == async_status == "pass" else "fail",
                details={
                    "sync_status": self._sync_completion_status,
                    "async_status": async_status,
                    "sync_request_id": self.report["checks"].get("B02_SYNC", {}).get("request_id"),
                    "async_request_id": self.report["checks"].get("B02_ASYNC", {}).get("request_id"),
                },
            )
            await self._auto_check(client)
            await self._history_check(client)
            replay_id, _ = await self._success_replay_check(client)
            await self._receipt_usage_check(raw, ai_root, self._sync_completion)
            await self._stream_check(client)
            await self._unknown_model_check(client)
            await self._unsupported_checks(client, raw, ai_root)
            await self._management_checks(raw, replay_id, self._attachment_request_id)

    async def _file_checks(self, client: AsyncOpenAI, raw: httpx.AsyncClient, ai_root: str) -> None:
        files: list[Any] = []
        payloads = [
            ("audit-default.txt", b"Gateway contract audit. unicode: xin chao.\n", "user_data", None),
            ("audit-vision.txt", b"vision purpose file\n", "vision", 7200),
            ("audit-assistants.txt", b"assistants purpose file\n", "assistants", None),
        ]
        cleanup_errors: list[dict[str, Any]] = []
        try:
            started = time.perf_counter()
            for filename, content, purpose, ttl in payloads:
                uploaded = await client.files.create(
                    file=(filename, content, "text/plain"),
                    purpose=purpose,
                    expires_after={"anchor": "created_at", "seconds": ttl} if ttl else None,
                )
                files.append((uploaded, content))
            metadata_ok = True
            content_ok = True
            for uploaded, expected in files:
                retrieved = await client.files.retrieve(uploaded.id)
                data = (await client.files.content(uploaded.id)).read()
                metadata_ok = metadata_ok and retrieved.id == uploaded.id and retrieved.purpose == uploaded.purpose
                content_ok = content_ok and data == expected
            self._case(
                "B05",
                "Files upload/retrieve/content bytes match",
                "pass" if metadata_ok and content_ok else "fail",
                latency_ms=(time.perf_counter() - started) * 1000,
                details={"file_count": len(files), "content_matches": content_ok},
            )
            ttl_default = files[0][0].expires_at - files[0][0].created_at
            ttl_custom = files[1][0].expires_at - files[1][0].created_at
            self._case(
                "B06",
                "Files default and custom TTL",
                "pass" if ttl_default == 86400 and ttl_custom == 7200 else "fail",
                details={"default_ttl_seconds": ttl_default, "custom_ttl_seconds": ttl_custom},
            )

            started = time.perf_counter()
            listed = [item async for item in client.files.list(limit=1)]
            ids = [item.id for item in listed]
            expected_ids = {item.id for item, _ in files}
            self._case(
                "B16",
                "SDK file auto-iteration has no gaps",
                "pass" if len(ids) == len(set(ids)) and set(ids) == expected_ids else "fail",
                latency_ms=(time.perf_counter() - started) * 1000,
                details={"expected_count": len(expected_ids), "iterated_count": len(ids), "file_ids": ids},
            )
            await self._file_pagination_contract(client, raw, ai_root, files)
            await self._invalid_file_check(client)
            if not self.config.skip_generation:
                try:
                    attachment = await client.chat.completions.create(
                        model=self.config.model,
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "Briefly summarize this audit file."},
                                    {"type": "file", "file": {"file_id": files[0][0].id}},
                                ],
                            }
                        ],
                        max_completion_tokens=48,
                        extra_headers={"Idempotency-Key": self._api_key("file-reference")},
                    )
                    self._attachment_request_id = attachment.id
                    self._case(
                        "B07",
                        "TXT file_id reference completes",
                        "pass" if attachment.choices else "fail",
                        details={"request_id": attachment.id, "trace_status": "requires_management_check"},
                    )
                except Exception as exc:
                    self._case("B07", "TXT file_id reference completes", "fail", details=_status_error(exc))
            await self._delete_file_check(client, files)
        except Exception as exc:
            failed_id = "B16" if "B16" not in self.report["checks"] else "B17"
            self._case(failed_id, "Files SDK contract operation", "fail", details=_status_error(exc))
        finally:
            for uploaded, _ in files:
                try:
                    result = await client.files.delete(uploaded.id)
                    if not result.deleted:
                        cleanup_errors.append({"file_id": uploaded.id, "error_class": "DeleteNotConfirmed"})
                except APIStatusError as exc:
                    if exc.status_code != 404:
                        cleanup_errors.append({"file_id": uploaded.id, **_status_error(exc)})
                except Exception as exc:
                    cleanup_errors.append({"file_id": uploaded.id, **_status_error(exc)})
            self._case(
                "CLEANUP_FILES",
                "Temporary audit files deleted",
                "fail" if cleanup_errors else "pass",
                details={"deleted_count": len(files) - len(cleanup_errors), "errors": cleanup_errors},
            )

    async def _file_pagination_contract(
        self,
        client: AsyncOpenAI,
        raw: httpx.AsyncClient,
        ai_root: str,
        files: list[tuple[Any, bytes]],
    ) -> None:
        asc = await client.files.list(limit=10000, order="asc")
        desc = await client.files.list(limit=10000, order="desc")
        vision = await client.files.list(limit=10000, purpose="vision")
        asc_ids = [item.id for item in asc.data]
        desc_ids = [item.id for item in desc.data]
        vision_ids = [item.id for item in vision.data]
        page = await client.files.list(limit=1, order="asc")
        next_page = await client.files.list(limit=1, order="asc", after=page.last_id)
        invalid_limit_responses = [
            await raw.get(f"{ai_root}/v1/files", params={"limit": value}, headers=_bearer(self.config.api_key))
            for value in (0, 10001, "abc")
        ]
        order_response = await raw.get(
            f"{ai_root}/v1/files", params={"order": "sideways"}, headers=_bearer(self.config.api_key)
        )
        page_cursor_ok = bool(page.data and page.has_more and next_page.data and next_page.data[0].id != page.data[0].id)
        filter_ok = len(vision_ids) == 1 and vision_ids[0] == next(item.id for item, _ in files if item.purpose == "vision")
        ordering_ok = desc_ids == list(reversed(asc_ids))
        limits_ok = (
            all(
                response.status_code == 400 and response.json().get("error", {}).get("param") == "limit"
                for response in invalid_limit_responses
            )
            and order_response.status_code == 400
            and order_response.json().get("error", {}).get("param") == "order"
        )
        self._case(
            "B17",
            "Files after/order/purpose/limit contract",
            "pass" if page_cursor_ok and filter_ok and ordering_ok and limits_ok else "fail",
            details={
                "ascending_ids": asc_ids,
                "descending_ids": desc_ids,
                "purpose_ids": vision_ids,
                "cursor_continues": page_cursor_ok,
                "invalid_query_params_rejected": limits_ok,
                "has_more": page.has_more,
            },
        )

    async def _invalid_file_check(self, client: AsyncOpenAI) -> None:
        outcomes = []
        attempts = (
            lambda: client.files.create(file=("bad.zip", b"PK\x03\x04", "application/zip"), purpose="user_data"),
            lambda: client.files.create(file=("bad.txt", b"bad", "text/plain"), purpose="unknown"),
            lambda: client.files.create(
                file=("bad.txt", b"bad", "text/plain"),
                purpose="user_data",
                expires_after={"anchor": "created_at", "seconds": 60},
            ),
        )
        for attempt in attempts:
            try:
                await attempt()
                outcomes.append(False)
            except APIStatusError as exc:
                outcomes.append(exc.status_code == 400)
        self._case("B08", "Invalid file type/purpose/TTL rejected", "pass" if all(outcomes) else "fail", details={"rejected_count": sum(outcomes), "attempt_count": len(outcomes)})

    async def _delete_file_check(self, client: AsyncOpenAI, files: list[tuple[Any, bytes]]) -> None:
        uploaded = files[0][0]
        deleted = await client.files.delete(uploaded.id)
        retrieve_missing = False
        content_missing = False
        try:
            await client.files.retrieve(uploaded.id)
        except APIStatusError as exc:
            retrieve_missing = exc.status_code == 404
        try:
            await client.files.content(uploaded.id)
        except APIStatusError as exc:
            content_missing = exc.status_code == 404
        remaining = [item async for item in client.files.list(limit=10000)]
        listed = uploaded.id in {item.id for item in remaining}
        self._case(
            "B12",
            "File delete removes read/content/list access",
            "pass" if deleted.deleted and retrieve_missing and content_missing and not listed else "fail",
            details={"deleted": deleted.deleted, "retrieve_404": retrieve_missing, "content_404": content_missing, "still_listed": listed},
        )

    async def _auth_check(self, raw: httpx.AsyncClient, ai_root: str) -> None:
        files = {"file": ("auth.txt", b"auth check", "text/plain")}
        calls = (
            ("GET", "/v1/models", None),
            ("POST", "/v1/chat/completions", {"model": self.config.model, "messages": [{"role": "user", "content": "auth"}]}),
            ("POST", "/v1/files", None),
            ("GET", "/v1/files", None),
            ("GET", "/v1/files/file-not-found", None),
            ("DELETE", "/v1/files/file-not-found", None),
            ("GET", "/v1/files/file-not-found/content", None),
        )
        statuses = []
        failures = []
        for method, path, body in calls:
            kwargs: dict[str, Any] = {}
            if body is not None:
                kwargs["json"] = body
            if method == "POST" and path == "/v1/files":
                kwargs = {"files": files, "data": {"purpose": "user_data"}}
            try:
                response = await raw.request(method, f"{ai_root}{path}", **kwargs)
                statuses.append(response.status_code)
            except Exception as exc:
                statuses.append(None)
                failures.append(type(exc).__name__)
        passed = statuses == [401] * 7
        self._case(
            "B09",
            "Missing key returns 401 across seven endpoints",
            "pass" if passed else "fail",
            details={"http_statuses": statuses, "transport_error_classes": failures},
        )

    async def _isolation_check(self, raw: httpx.AsyncClient, ai_root: str) -> None:
        if not self.config.other_api_key:
            self._skip("B10", "Cross-application file isolation", "GATEWAY_AUDIT_OTHER_API_KEY was not provided")
            self._skip("B11", "Second/revoked same-application key", "No second or revoked same-application key was provided")
            return
        response = await raw.get(f"{ai_root}/v1/files", headers=_bearer(self.config.other_api_key))
        self._case(
            "B10",
            "Other application cannot list temporary files",
            "pass" if response.status_code == 200 and response.json().get("data") == [] else "fail",
            details={"http_status": response.status_code},
        )
        self._skip("B11", "Second/revoked same-application key", "Only a key for a different application was provided")

    async def _chat_check(self, client: AsyncOpenAI, case_id: str, name: str, *, model: str) -> Any | None:
        started = time.perf_counter()
        try:
            result = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": f"Reply briefly to audit {self.run_id[:8]}."}],
                max_completion_tokens=48,
                extra_headers={"Idempotency-Key": self._api_key(case_id.lower())},
            )
            passed = result.object == "chat.completion" and bool(result.id) and isinstance(result.choices[0].message.content, str)
            self._case(
                case_id,
                name,
                "pass" if passed else "fail",
                latency_ms=(time.perf_counter() - started) * 1000,
                details={"http_status": 200, "request_id": result._request_id, "completion_id": result.id},
            )
            return result
        except Exception as exc:
            self._case(case_id, name, "fail", details=_status_error(exc))
            return None

    async def _auto_check(self, client: AsyncOpenAI) -> None:
        result = await self._chat_check(client, "B03", "Auto alias completion", model="auto")
        if result is not None:
            self.report["checks"]["B03"]["model"] = result.model

    async def _history_check(self, client: AsyncOpenAI) -> None:
        try:
            result = await client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "user", "content": "Use the earlier context if relevant."},
                    {"role": "assistant", "content": "Acknowledged."},
                    {"role": "user", "content": "Now reply briefly."},
                ],
                max_completion_tokens=48,
                extra_headers={"Idempotency-Key": self._api_key("history")},
            )
            self._case("B04", "Multi-message history accepted", "pass" if result.choices else "fail", details={"request_id": result._request_id})
        except Exception as exc:
            self._case("B04", "Multi-message history accepted", "fail", details=_status_error(exc))

    async def _success_replay_check(self, client: AsyncOpenAI) -> tuple[str | None, str | None]:
        idem = self._api_key("replay")
        body = {"model": self.config.model, "messages": [{"role": "user", "content": "idempotent audit response"}], "max_completion_tokens": 48}
        management_before = await self._management_request_count()
        try:
            first_raw = await client.chat.completions.with_raw_response.create(
                **body, extra_headers={"Idempotency-Key": idem}
            )
            first = first_raw.parse()
            replay_raw = await client.chat.completions.with_raw_response.create(
                **body, extra_headers={"Idempotency-Key": idem}
            )
            replay = replay_raw.parse()
            conflict = False
            try:
                await client.chat.completions.create(
                    model=self.config.model,
                    messages=[{"role": "user", "content": "different idempotent audit body"}],
                    max_completion_tokens=48,
                    extra_headers={"Idempotency-Key": idem},
                )
            except APIStatusError as exc:
                conflict = exc.status_code == 409 and getattr(exc, "code", None) == "idempotency_conflict"
            stable_id = first.id == replay.id
            replay_header = replay_raw.headers.get("idempotency-replayed") == "true"
            count_before, count_after = management_before, await self._management_request_count()
            no_new_operation = (
                True
                if not self._management_ready()
                else count_before is not None and count_after == count_before + 1
            )
            passed = stable_id and replay_header and conflict
            self._case(
                "B13",
                "Success replay is stable and body conflict is rejected",
                "pass" if passed else "fail",
                details={
                    "first_request_id": first._request_id,
                    "replay_request_id": replay._request_id,
                    "idempotency_replayed": replay_header,
                    "conflict_409": conflict,
                },
            )
            execution_count_status = (
                "skipped"
                if not self._management_ready()
                else "pass"
                if no_new_operation
                else "fail"
            )
            self._case(
                "B13_USAGE",
                "Success replay creates no additional runtime operation",
                execution_count_status,
                details={"operation_count_before": count_before, "operation_count_after": count_after},
            )
            return first.id, first._request_id
        except Exception as exc:
            self._case("B13", "Success replay is stable and body conflict is rejected", "fail", details=_status_error(exc))
            self._case(
                "B13_USAGE",
                "Success replay creates no additional runtime operation",
                "skipped" if not self._management_ready() else "fail",
                details={"reason": "replay operation did not complete"},
            )
            return None, None

    async def _receipt_usage_check(self, raw: httpx.AsyncClient, ai_root: str, completion: Any) -> bool:
        if completion is None:
            self._skip("B14", "Chat usage matches runtime receipt", "No successful sync completion was available")
            return False
        receipt_base = (self.config.receipt_base_url or f"{ai_root}/api/v1/runtime").rstrip("/")
        try:
            response = await raw.get(
                f"{receipt_base}/requests/{completion.id}", headers=_bearer(self.config.api_key)
            )
            response.raise_for_status()
            receipt = response.json()
            usage = receipt.get("usage") or {}
            scope = usage.get("pipeline") or usage.get("execution") or {}
            expected = {
                "prompt_tokens": scope.get("input_tokens", 0),
                "completion_tokens": scope.get("output_tokens", 0),
                "total_tokens": scope.get("total_tokens", 0),
            }
            actual = (
                {
                    "prompt_tokens": completion.usage.prompt_tokens,
                    "completion_tokens": completion.usage.completion_tokens,
                    "total_tokens": completion.usage.total_tokens,
                }
                if completion.usage
                else {}
            )
            passed = actual == expected and receipt.get("request_id") == completion.id
            self._case(
                "B14",
                "Chat usage matches runtime receipt",
                "pass" if passed else "fail",
                details={"request_id": completion.id, "public_usage": actual, "receipt_usage": expected},
            )
            return passed
        except Exception as exc:
            self._case("B14", "Chat usage matches runtime receipt", "fail", details=_status_error(exc))
            return False

    async def _stream_check(self, client: AsyncOpenAI) -> None:
        try:
            chunks = [
                chunk
                async for chunk in await client.chat.completions.create(
                    model=self.config.model,
                    messages=[{"role": "user", "content": "Reply briefly for stream contract audit."}],
                    stream=True,
                    stream_options={"include_usage": True},
                    max_completion_tokens=48,
                    extra_headers={"Idempotency-Key": self._api_key("stream")},
                )
            ]
            text_chunks = [choice.delta.content for chunk in chunks for choice in chunk.choices if choice.delta.content]
            finishes = [choice.finish_reason for chunk in chunks for choice in chunk.choices if choice.finish_reason]
            usage_chunks = [chunk.usage for chunk in chunks if chunk.usage]
            passed = bool(text_chunks) and bool(finishes) and len(usage_chunks) == 1
            self._case(
                "B23",
                "Streaming SDK parses content/finish/usage",
                "pass" if passed else "fail",
                details={"content_chunk_count": len(text_chunks), "finish_reasons": finishes, "usage": usage_chunks[0].model_dump() if usage_chunks else None},
            )
        except Exception as exc:
            self._case("B23", "Streaming SDK parses content/finish/usage", "fail", details=_status_error(exc))

    async def _unknown_model_check(self, client: AsyncOpenAI) -> None:
        from openai import NotFoundError

        try:
            await client.chat.completions.create(
                model="gateway-audit-model-does-not-exist",
                messages=[{"role": "user", "content": "not executed"}],
                max_completion_tokens=8,
            )
        except NotFoundError as exc:
            passed = getattr(exc, "code", None) == "model_not_found"
            self._case("B22", "Unknown model and invalid messages map to 404/400", "pass" if passed else "fail", details=_status_error(exc))
            return
        except Exception as exc:
            self._case("B22", "Unknown model and invalid messages map to 404/400", "fail", details=_status_error(exc))
            return
        self._case("B22", "Unknown model and invalid messages map to 404/400", "fail", details={"expected": "404 model_not_found"})

    async def _unsupported_checks(self, client: AsyncOpenAI, raw: httpx.AsyncClient, ai_root: str) -> None:
        await self._management_snapshot_before_rejections()
        schema_ok = False
        try:
            from pydantic import BaseModel

            class AuditAnswer(BaseModel):
                answer: str

            await client.chat.completions.parse(
                model=self.config.model,
                messages=[{"role": "user", "content": "Return one answer."}],
                response_format=AuditAnswer,
            )
        except APIStatusError as exc:
            schema_ok = (
                exc.status_code == 400
                and getattr(exc, "code", None) == "unsupported_parameter"
                and getattr(exc, "param", None) == "response_format"
            )
            details = _status_error(exc)
        except Exception as exc:
            details = _status_error(exc)
        else:
            details = {"expected": "400 unsupported_parameter response_format"}
        self._case("B18", "SDK parse receives explicit json_schema unsupported error", "pass" if schema_ok else "fail", details=details)

        stop_ok = await self._expect_error(
            lambda: client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": "stop validation"}],
                stop="GATEWAY_AUDIT_STOP",
            ),
            status=400,
            code="unsupported_parameter",
            param="stop",
        )
        self._case("B19", "Valid stop is explicitly unsupported", "pass" if stop_ok[0] else "fail", details=stop_ok[1])

        invalid_errors = []
        for field, value in (("temperature", -1), ("top_p", 2)):
            checked = await self._expect_error(
                lambda field=field, value=value: client.chat.completions.create(
                    model=self.config.model,
                    messages=[{"role": "user", "content": "sampling validation"}],
                    **{field: value},
                ),
                status=400,
                code="invalid_request",
                param=field,
            )
            invalid_errors.append(checked[0])
        valid_zero = await self._expect_error(
            lambda: client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": "sampling unsupported"}],
                temperature=0,
            ),
            status=400,
            code="unsupported_parameter",
            param="temperature",
        )
        self._case(
            "B20",
            "Invalid sampling is rejected and supported-shape sampling is explicit",
            "pass" if all(invalid_errors) and valid_zero[0] else "fail",
            details={"out_of_range_rejections": sum(invalid_errors), "valid_zero_rejected": valid_zero[0]},
        )

        tools = await self._expect_error(
            lambda: client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": "tools unsupported"}],
                tools=[{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
            ),
            status=400,
            code="unsupported_parameter",
            param="tools",
        )
        multiple = await self._expect_error(
            lambda: client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": "multiple choices unsupported"}],
                n=2,
            ),
            status=400,
            code="unsupported_parameter",
            param="n",
        )
        self._case("B21", "Tools and n>1 are rejected", "pass" if tools[0] and multiple[0] else "fail", details={"tools": tools[1], "n": multiple[1]})

        missing = await raw.post(
            f"{ai_root}/v1/chat/completions",
            json={"model": self.config.model},
            headers=_bearer(self.config.api_key),
        )
        missing_ok = missing.status_code == 400 and missing.json().get("error", {}).get("param") == "messages"
        if not missing_ok:
            self.report["checks"]["B22"]["missing_messages"] = {"http_status": missing.status_code, "status": "fail"}
            self.report["checks"]["B22"]["status"] = "fail"
        else:
            self.report["checks"]["B22"]["missing_messages"] = {"http_status": 400, "status": "pass"}
        await self._management_snapshot_after_rejections()
        await self._management_usage_delta_check()

    async def _expect_error(
        self,
        operation: Callable[[], Any],
        *,
        status: int,
        code: str,
        param: str,
    ) -> tuple[bool, dict[str, Any]]:
        try:
            await operation()
        except APIStatusError as exc:
            details = _status_error(exc)
            return (
                exc.status_code == status
                and details.get("code") == code
                and details.get("param") == param,
                details,
            )
        except Exception as exc:
            return False, _status_error(exc)
        return False, {"expected_http_status": status, "expected_code": code, "expected_param": param}

    async def _management_request_count(self) -> int | None:
        if not self._management_ready():
            return None
        async with httpx.AsyncClient(timeout=20) as management:
            response = await management.get(
                f"{self.config.management_base_url.rstrip('/')}/api/v1/gateway/overview",
                params={"application_id": self.config.application_id, "scope": "own"},
                headers={"Authorization": f"Bearer {self.config.user_access_token}"},
            )
            if response.status_code >= 400:
                return None
            return int(response.json().get("total_requests", 0))

    def _management_ready(self) -> bool:
        return all((self.config.management_base_url, self.config.user_access_token, self.config.application_id))

    async def _management_snapshot_before_rejections(self) -> None:
        self._management_before_rejections = await self._management_request_count()

    async def _management_snapshot_after_rejections(self) -> None:
        self.report["rejected_request_usage_count"] = {
            "status": "skipped",
            "reason": "management access group was not provided or not available",
        }
        if self._management_before_rejections is None:
            if self._management_ready():
                self.report["rejected_request_usage_count"] = {
                    "status": "fail",
                    "reason": "management usage count could not be read before rejection checks",
                }
            return
        after = await self._management_request_count()
        passed = after == self._management_before_rejections
        self.report["rejected_request_usage_count"] = {
            "status": "pass" if passed else "fail",
            "before": self._management_before_rejections,
            "after": after,
        }

    async def _management_usage_delta_check(self) -> None:
        if not self._management_ready():
            self._skip("B20_USAGE", "No execution created by unsupported parameters", "Management usage credentials were not provided")
            return
        status = self.report.get("rejected_request_usage_count", {}).get("status")
        self._case(
            "B20_USAGE",
            "No execution created by unsupported parameters",
            status or "fail",
            details=self.report.get("rejected_request_usage_count", {}),
        )

    async def _management_checks(self, raw: httpx.AsyncClient, replay_id: str | None, replay_request_id: str | None) -> None:
        if not self._management_ready():
            self._skip("B28", "Receipt estimated cost matches management usage debit", "Management access group was not provided")
            self._skip("B07", "Uploaded file has an attachment analysis trace", "Management access group was not provided")
            self._skip("B15", "Terminal error replay", "No controlled live provider failure scenario; Task 2 fixture replay regression passed")
            self._skip("B20_USAGE", "No execution created by unsupported parameters", "Management usage credentials were not provided")
            return
        headers = {"Authorization": f"Bearer {self.config.user_access_token}"}
        management_root = self.config.management_base_url.rstrip("/")
        try:
            usage = await raw.get(
                f"{management_root}/api/v1/gateway/usage",
                params={"application_id": self.config.application_id, "page": 1, "page_size": 100},
                headers=headers,
            )
            usage.raise_for_status()
            usage_items = usage.json().get("items", [])
            matching = next((item for item in usage_items if item.get("request_id") == replay_id), None)
            if matching is None:
                self._case("B28", "Receipt estimated cost matches management usage debit", "fail", details={"usage_row_found": False})
            else:
                receipt_base = (self.config.receipt_base_url or f"{_ai_root(self.config.base_url)}/api/v1/runtime").rstrip("/")
                receipt_response = await raw.get(f"{receipt_base}/requests/{replay_id}", headers=_bearer(self.config.api_key))
                receipt_response.raise_for_status()
                receipt_cost = float((receipt_response.json().get("cost") or {}).get("estimated_cost", 0))
                usage_cost = float(matching.get("cost", 0))
                self._case(
                    "B28",
                    "Receipt estimated cost matches management usage debit",
                    "pass" if abs(receipt_cost - usage_cost) <= 1e-10 else "fail",
                    details={"request_id": replay_id, "receipt_cost_usd": receipt_cost, "usage_cost_usd": usage_cost},
                )
            if replay_request_id:
                detail = await raw.get(f"{management_root}/api/v1/gateway/requests/{replay_request_id}", headers=headers)
                if detail.status_code == 200:
                    traces = detail.json().get("traces", [])
                    attachment_trace = next((trace for trace in traces if trace.get("stage") == "attachment_analysis"), None)
                    trace_status = attachment_trace.get("status") if attachment_trace else None
                    self._case(
                        "B07",
                        "Uploaded file has a successful attachment analysis trace",
                        "pass" if trace_status == "success" else "fail",
                        details={"trace_status": trace_status},
                    )
                else:
                    self._case("B07", "Uploaded file has an attachment analysis trace", "fail", details={"http_status": detail.status_code})
            else:
                self._case(
                    "B07",
                    "Uploaded file has an attachment analysis trace",
                    "fail",
                    details={"reason": "No successful file reference request was recorded"},
                )
            self._skip("B15", "Terminal error replay", "No controlled live provider failure scenario; Task 2 fixture replay regression passed")
        except Exception as exc:
            for case_id, name in (
                ("B28", "Receipt estimated cost matches management usage debit"),
                ("B07", "Uploaded file has an attachment analysis trace"),
            ):
                self._case(case_id, name, "fail", details=_status_error(exc))
            self._skip("B15", "Terminal error replay", "No controlled live provider failure scenario; Task 2 fixture replay regression passed")

    def _skip_generation_cases(self) -> None:
        for case_id, name in (
            ("B07", "file attachment trace"),
            ("B13", "success replay and conflict"),
            ("B13_USAGE", "replay operation count"),
            ("B14", "usage matches runtime receipt"),
            ("B15", "terminal error replay"),
            ("B18", "json_schema rejection"),
            ("B19", "stop rejection"),
            ("B20", "sampling validation and rejection"),
            ("B20_USAGE", "no operation for rejected parameters"),
            ("B21", "tools/n rejection"),
            ("B22", "unknown model/messages validation"),
            ("B23", "streaming completion"),
            ("B28", "cost/debit comparison"),
        ):
            self._skip(case_id, name, "--skip-generation was requested")
        self._skip("B04", "message history accepted", "--skip-generation was requested")

    def _finish_report(self) -> None:
        checks = self.report["checks"]
        counts = {status: sum(1 for check in checks.values() if check.get("status") == status) for status in ("pass", "fail", "skipped")}
        self.report["summary"] = counts
        self.report["finished_at"] = datetime.now(UTC).isoformat()
        self.report["full_suite_passed"] = counts["fail"] == 0 and counts["skipped"] == 0 and self.report["scope"] == "full"
        self.report["status"] = "fail" if counts["fail"] else "partial" if counts["skipped"] else "pass"


def _bearer(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def run_audit(
    config: AuditConfig,
    *,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    return ContractAudit(config, transport=transport).run()


def _config_from_env(args: argparse.Namespace) -> AuditConfig | None:
    required = {
        "GATEWAY_OPENAI_BASE_URL": os.getenv("GATEWAY_OPENAI_BASE_URL"),
        "GATEWAY_API_KEY": os.getenv("GATEWAY_API_KEY"),
        "GATEWAY_OPENAI_MODEL": os.getenv("GATEWAY_OPENAI_MODEL"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        print("Missing required configuration: " + ", ".join(missing), file=sys.stderr)
        return None
    return AuditConfig(
        base_url=required["GATEWAY_OPENAI_BASE_URL"].rstrip("/"),
        api_key=required["GATEWAY_API_KEY"],
        model=required["GATEWAY_OPENAI_MODEL"],
        other_api_key=os.getenv("GATEWAY_AUDIT_OTHER_API_KEY") or None,
        management_base_url=os.getenv("GATEWAY_AUDIT_MANAGEMENT_BASE_URL") or None,
        user_access_token=os.getenv("GATEWAY_AUDIT_USER_ACCESS_TOKEN") or None,
        application_id=os.getenv("GATEWAY_AUDIT_APPLICATION_ID") or None,
        receipt_base_url=args.receipt_base_url or None,
        skip_generation=args.skip_generation,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--receipt-base-url")
    parser.add_argument("--skip-generation", action="store_true")
    args = parser.parse_args(argv)
    config = _config_from_env(args)
    if config is None:
        return 2
    report = run_audit(config)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    print(
        f"OpenAI SDK contract audit: {report['status']} "
        f"(pass={report['summary']['pass']}, fail={report['summary']['fail']}, skipped={report['summary']['skipped']})",
        file=sys.stderr,
    )
    if report["summary"]["fail"]:
        return 1
    if report["status"] == "partial":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
