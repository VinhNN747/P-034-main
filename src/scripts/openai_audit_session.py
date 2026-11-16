"""In-memory management session for the two-application SDK audit.

Credentials are accepted as arguments/environment values, never serialized.
Only resources registered by this session are eligible for cleanup.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx


def evidence_context():
    """Hash public implementation files only; never read env or credentials."""
    import hashlib
    import subprocess
    from datetime import UTC, datetime
    from importlib.metadata import version
    from pathlib import Path
    paths = sorted([*Path('src').rglob('*.py'),
                    Path('src/requirements-ai.txt'), Path('src/deploy/nginx.conf')])
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path).encode() + b'\0' + path.read_bytes() + b'\0')
    return {'captured_at_utc': datetime.now(UTC).isoformat(),
            'base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
            'implementation_sha256': digest.hexdigest(), 'python_sdk': version('openai'),
            'source_state': 'uncommitted-working-tree' if subprocess.check_output(
                ['git', 'status', '--porcelain'], text=True) else 'committed'}


class AuditPrerequisiteError(RuntimeError):
    """Missing permission, ownership, or configuration; never counts as pass."""


@dataclass(repr=False)
class AuditSession:
    supabase_url: str
    publishable_key: str = field(repr=False)
    management_url: str
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, init=False, repr=False)
    applications: dict[str, str] = field(default_factory=dict, init=False)
    cleanup_results: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __enter__(self):
        self.client = httpx.Client(transport=self.transport, timeout=30)
        self._cleanup: list[tuple[str, Callable[[], Any]]] = []
        return self

    def __exit__(self, *_):
        try:
            for label, callback in reversed(self._cleanup):
                try:
                    callback()
                except Exception:
                    self.cleanup_results.append({"resource": label, "status": "fail"})
                else:
                    self.cleanup_results.append({"resource": label, "status": "pass"})
        finally:
            self.access_token = None
            self.client.close()

    def login(self, email: str, password: str) -> None:
        response = self.client.post(
            self.supabase_url.rstrip("/") + "/auth/v1/token?grant_type=password",
            headers={"apikey": self.publishable_key},
            json={"email": email, "password": password},
        )
        if response.status_code != 200:
            raise AuditPrerequisiteError(f"Account login failed (HTTP {response.status_code})")
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise AuditPrerequisiteError("Login did not return an access token")
        self.access_token = token

    def management(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self.access_token is None:
            raise AuditPrerequisiteError("Account login is required")
        response = self.client.request(
            method,
            self.management_url.rstrip("/") + path,
            headers={"Authorization": "Bearer " + self.access_token},
            **kwargs,
        )
        if response.status_code >= 400:
            raise AuditPrerequisiteError(f"Management prerequisite failed (HTTP {response.status_code})")
        return response

    def _items(self, path: str) -> list[dict]:
        items = []
        page = 1
        while True:
            body = self.management("GET", path, params={"page": page, "page_size": 100}).json()
            batch = body.get("items", [])
            items.extend(batch)
            if not batch or len(items) >= body.get("total", len(items)):
                return items
            page += 1

    def resolve_application(self, label: str, api_key: str, *, expected_id: str | None = None) -> str:
        matches = []
        for application in self._items("/applications"):
            # Each management key-list route enforces account ownership.
            keys = self._items(f"/applications/{application['id']}/api-keys")
            if any(k.get("key_prefix") == api_key[:12] and not k.get("revoked") for k in keys):
                matches.append(application["id"])
        if len(matches) != 1 or (expected_id is not None and matches[0] != expected_id):
            raise AuditPrerequisiteError("Application ownership/key mapping could not be verified")
        if matches[0] in self.applications.values() and self.applications.get(label) != matches[0]:
            raise AuditPrerequisiteError("Two application labels resolve to the same application")
        self.applications[label] = matches[0]
        return matches[0]

    def register_cleanup(self, label: str, callback: Callable[[], Any]) -> None:
        self._cleanup.append((label, callback))

    @staticmethod
    def audit_case(case_id: str, evidence_kind: str, assertions: dict[str, bool], *, prerequisite=True) -> dict:
        # Booleans with known assertion names prevent arbitrary payload/secret leakage.
        allowed = {"ownership", "isolation", "envelope", "request_id", "usage", "replay", "cleanup"}
        if any(key not in allowed or type(value) is not bool for key, value in assertions.items()):
            raise ValueError("Evidence accepts only named boolean assertions")
        return {
            "case_id": case_id,
            "evidence_kind": evidence_kind,
            "assertions": assertions,
            "status": "skipped"
            if not prerequisite
            else ("pass" if assertions and all(assertions.values()) else "fail"),
        }
