"""Fail-closed preflight and cost ledger for a small paid latency smoke run.

This tool deliberately makes no provider calls in dry-run mode. The live
transport is deployment-specific (Playground authentication, API routing,
model allow-list and billing lookup), so this script refuses to send traffic
until that adapter is explicitly implemented rather than guessing an endpoint
or claiming an unknown bill is zero.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

MAX_USER_CEILING_USD = Decimal("0.20")
DISPATCH_SHARE = Decimal("0.80")
_FORBIDDEN_FIELDS = {
    "key", "apikey", "applicationkey", "applicationkeyvalue", "authorization", "secret",
    "body", "payload", "prompt", "messages", "content", "usertoken", "accesstoken", "bearertoken",
}


class ManifestError(ValueError):
    """The campaign manifest is missing an auditable cost or isolation bound."""


class BudgetExceededError(ValueError):
    """A request cannot be safely dispatched within the reserved budget."""


class LiveExecutionBlockedError(RuntimeError):
    """The current deployment has no verified live smoke transport adapter."""


@dataclass(slots=True)
class Reservation:
    reservation_id: str
    amount_usd: Decimal
    generation_requests: int
    status: str = "reserved"


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ManifestError(f"{label} must be a finite non-negative decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ManifestError(f"{label} must be a finite non-negative decimal") from exc
    if not result.is_finite() or result < 0:
        raise ManifestError(f"{label} must be a finite non-negative decimal")
    return result


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManifestError(f"{label} must be a non-negative integer")
    return value


def _reject_sensitive_fields(value: Any, path: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in _FORBIDDEN_FIELDS:
                raise ManifestError(f"forbidden field at {path}.{key}")
            _reject_sensitive_fields(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_sensitive_fields(nested, f"{path}[{index}]")


class BudgetLedger:
    """Thread-safe reservation ledger with a hard user cap and 20% reserve."""

    def __init__(self, max_cost_usd: Decimal | str = Decimal("0.20"), *, max_generation_requests: int = 8):
        maximum = _decimal(max_cost_usd, "max_cost_usd")
        if maximum <= 0 or maximum > MAX_USER_CEILING_USD:
            raise BudgetExceededError("max_cost_usd must be greater than zero and no more than $0.20")
        if isinstance(max_generation_requests, bool) or not isinstance(max_generation_requests, int) or max_generation_requests < 0:
            raise BudgetExceededError("max_generation_requests must be a non-negative integer")
        self.max_cost_usd = maximum
        self.dispatch_limit_usd = maximum * DISPATCH_SHARE
        self.reserve_usd = maximum - self.dispatch_limit_usd
        self.max_generation_requests = max_generation_requests
        self._settled_usd = Decimal("0")
        self._unknown_usd = Decimal("0")
        self._outstanding: dict[str, Reservation] = {}
        self._generation_requests = 0
        self._lock = threading.Lock()

    @property
    def settled_usd(self) -> Decimal:
        with self._lock:
            return self._settled_usd

    @property
    def unknown_usd(self) -> Decimal:
        with self._lock:
            return self._unknown_usd

    @property
    def outstanding_usd(self) -> Decimal:
        with self._lock:
            return sum((row.amount_usd for row in self._outstanding.values()), Decimal("0"))

    def reserve_request(self, estimated_upper_bound_usd: Decimal | str, *, generation_requests: int = 1) -> Reservation:
        amount = _decimal(estimated_upper_bound_usd, "estimated_upper_bound_usd")
        if isinstance(generation_requests, bool) or not isinstance(generation_requests, int) or generation_requests < 0:
            raise BudgetExceededError("generation_requests must be a non-negative integer")
        with self._lock:
            reserved = sum((row.amount_usd for row in self._outstanding.values()), Decimal("0"))
            if self._settled_usd + reserved + amount > self.dispatch_limit_usd:
                raise BudgetExceededError("reservation exceeds the 80% dispatch budget")
            if self._generation_requests + generation_requests > self.max_generation_requests:
                raise BudgetExceededError("reservation exceeds the generation request limit")
            reservation = Reservation(str(uuid4()), amount, generation_requests)
            self._outstanding[reservation.reservation_id] = reservation
            self._generation_requests += generation_requests
            return reservation

    def settle_request(self, reservation: Reservation, actual_usd: Decimal | str | None) -> None:
        with self._lock:
            current = self._outstanding.get(reservation.reservation_id)
            if current is not reservation or reservation.status != "reserved":
                raise BudgetExceededError("reservation is unknown or has already been settled")
            if actual_usd is None:
                reservation.status = "unknown"
                self._unknown_usd += reservation.amount_usd
                # Keep unknown usage reserved permanently for this campaign.
                return
            actual = _decimal(actual_usd, "actual_usd")
            if actual > reservation.amount_usd:
                reservation.status = "overrun"
                self._settled_usd += actual
                self._outstanding.pop(reservation.reservation_id)
                raise BudgetExceededError("actual cost exceeded its request reservation; stop the campaign")
            reservation.status = "settled"
            self._settled_usd += actual
            self._outstanding.pop(reservation.reservation_id)


def _validate_role(role: str, bounds: Any, case_id: str) -> tuple[Decimal, int]:
    if not isinstance(bounds, dict):
        raise ManifestError(f"{case_id}.{role} requires explicit role bounds")
    required = {
        "max_calls", "input_tokens_per_call", "output_tokens_per_call",
        "input_usd_per_million", "output_usd_per_million", "price_version",
    }
    missing = sorted(required - bounds.keys())
    if missing:
        raise ManifestError(f"{case_id}.{role} missing required bounds: {', '.join(missing)}")
    calls = _nonnegative_int(bounds["max_calls"], f"{case_id}.{role}.max_calls")
    input_tokens = _nonnegative_int(bounds["input_tokens_per_call"], f"{case_id}.{role}.input_tokens_per_call")
    output_tokens = _nonnegative_int(bounds["output_tokens_per_call"], f"{case_id}.{role}.output_tokens_per_call")
    input_price = _decimal(bounds["input_usd_per_million"], f"{case_id}.{role}.input_usd_per_million")
    output_price = _decimal(bounds["output_usd_per_million"], f"{case_id}.{role}.output_usd_per_million")
    price_version = bounds["price_version"]
    if not isinstance(price_version, str) or not price_version.strip():
        raise ManifestError(f"{case_id}.{role}.price_version is required")
    cost = calls * (
        Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price
    ) / Decimal(1_000_000)
    return cost, calls if role == "generation" else 0


def validate_manifest(
    manifest: Any,
    *,
    max_cost_usd: Decimal | str = Decimal("0.20"),
    max_generation_requests: int = 8,
    application_id: str | None = None,
    allow_blocked: bool = False,
) -> dict[str, Any]:
    _reject_sensitive_fields(manifest)
    if not isinstance(manifest, dict):
        raise ManifestError("campaign manifest must be a JSON object")
    if not isinstance(manifest.get("campaign_id"), str) or not manifest["campaign_id"].strip():
        raise ManifestError("campaign_id is required")
    manifest_app = manifest.get("application_id")
    if not isinstance(manifest_app, str) or not manifest_app.strip():
        raise ManifestError("application_id is required")
    if application_id is not None and manifest_app != application_id:
        raise ManifestError("manifest application_id does not match --application-id")
    isolation = manifest.get("isolation")
    live_blockers: list[str] = []
    if not isinstance(isolation, dict) or isolation.get("database_disposable") is not True:
        live_blockers.append("a disposable database is not verified")
    if not isinstance(isolation, dict) or isolation.get("comparison_disabled") is not True:
        live_blockers.append("comparison/evaluation workers are not verified disabled")
    if not allow_blocked and live_blockers:
        raise ManifestError(live_blockers[0])
    preflight = manifest.get("preflight", {})
    if preflight is not None and not isinstance(preflight, dict):
        raise ManifestError("preflight must be a JSON object")
    for check_name, description in (
        ("base_url_verified", "target base URL is not verified"),
        ("application_key_verified", "application key/application binding is not verified"),
        ("playground_user_token_verified", "Playground user session is not verified"),
        ("provider_price_versions_verified", "provider price versions are not verified"),
        ("billing_settlement_verified", "billing usage settlement is not verified"),
    ):
        if preflight and preflight.get(check_name) is not True:
            live_blockers.append(description)
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ManifestError("cases must be a non-empty list")

    max_cost = _decimal(max_cost_usd, "max_cost_usd")
    if max_cost <= 0 or max_cost > MAX_USER_CEILING_USD:
        raise ManifestError("max_cost_usd must be greater than zero and no more than $0.20")
    dispatch_limit = max_cost * DISPATCH_SHARE
    if isinstance(max_generation_requests, bool) or not isinstance(max_generation_requests, int) or max_generation_requests < 0:
        raise ManifestError("max_generation_requests must be a non-negative integer")

    case_ids: set[str] = set()
    summaries: list[dict[str, Any]] = []
    total = Decimal("0")
    generation_count = 0
    for case in cases:
        if not isinstance(case, dict):
            raise ManifestError("every case must be an object")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in case_ids:
            raise ManifestError("case_id must be non-empty and unique")
        case_ids.add(case_id)
        if case.get("retry_count") != 0:
            raise ManifestError(f"{case_id}: retries must be zero")
        if case.get("n") != 1:
            raise ManifestError(f"{case_id}: n must be one")
        roles = case.get("roles")
        if not isinstance(roles, dict) or "generation" not in roles:
            raise ManifestError(f"{case_id}: generation call bound is required")
        case_cost = Decimal("0")
        case_generation = 0
        for role, bounds in roles.items():
            role_cost, role_generation = _validate_role(str(role), bounds, case_id)
            if any(marker in str(bounds["price_version"]).lower() for marker in ("placeholder", "illustrative", "unknown")):
                live_blockers.append(f"{case_id}.{role} uses an unverified planning price")
            case_cost += role_cost
            case_generation += role_generation
        generation_role = roles["generation"]
        if generation_role["max_calls"] != 1:
            raise ManifestError(f"{case_id}: each case must have exactly one bounded generation request")
        total += case_cost
        generation_count += case_generation
        summaries.append({
            "case_id": case_id,
            "surface": case.get("surface"),
            "upper_bound_usd": str(case_cost),
            "generation_requests": case_generation,
            "roles": sorted(str(role) for role in roles),
        })

    if total > dispatch_limit:
        raise ManifestError("manifest upper bounds exceed the 80% dispatch budget")
    if generation_count > max_generation_requests:
        raise ManifestError("manifest exceeds the generation request limit")
    return {
        "campaign_id": manifest["campaign_id"],
        "application_id": manifest_app,
        "case_count": len(summaries),
        "generation_requests": generation_count,
        "dispatch_limit_usd": str(dispatch_limit),
        "reserve_usd": str(max_cost - dispatch_limit),
        "upper_bound_usd": str(total),
        "remaining_dispatch_budget_usd": str(dispatch_limit - total),
        "live_ready": not live_blockers,
        "live_blockers": sorted(set(live_blockers)),
        "cases": summaries,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--application-id")
    parser.add_argument("--campaign-manifest", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-cost-usd", default="0.20")
    parser.add_argument("--max-generation-requests", type=int, default=8)
    parser.add_argument("--output-dir", default="/tmp/latency-smoke-campaign")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest_path = Path(args.campaign_manifest)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        summary = validate_manifest(
            manifest,
            max_cost_usd=args.max_cost_usd,
            max_generation_requests=args.max_generation_requests,
            application_id=args.application_id,
            allow_blocked=args.dry_run,
        )
        if not args.dry_run:
            if not args.base_url:
                raise LiveExecutionBlockedError("--base-url is required for a live run")
            if not os.environ.get("LATENCY_SMOKE_APPLICATION_KEY") or not os.environ.get("LATENCY_SMOKE_USER_TOKEN"):
                raise LiveExecutionBlockedError("application key and Playground user token must be supplied via environment")
            # Never guess the application's two-backend routing, account scope,
            # provider prices, or usage settlement API. A future deployment
            # adapter can be enabled after these have been verified.
            raise LiveExecutionBlockedError(
                "no verified deployment transport/usage-settlement adapter is configured; no request was sent"
            )

        summary.update({
            "mode": "dry-run",
            "network_requests": 0,
            "paid_requests": 0,
            "secret_values_read": False,
        })
        summary["live_blockers"].append("dry-run does not verify endpoint credentials or billing")
        summary["live_ready"] = False
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(output_dir, 0o700)
        evidence = output_dir / "preflight.json"
        evidence.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(evidence, 0o600)
        print(json.dumps(summary, sort_keys=True))
        return 0
    except (OSError, json.JSONDecodeError, ManifestError, BudgetExceededError, LiveExecutionBlockedError) as exc:
        print(f"latency smoke preflight blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

