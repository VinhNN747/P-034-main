from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from src.scripts.latency_smoke_campaign import (
    BudgetExceededError,
    BudgetLedger,
    ManifestError,
    main,
    validate_manifest,
)


def _manifest() -> dict:
    role = {
        "max_calls": 1,
        "input_tokens_per_call": 100,
        "output_tokens_per_call": 50,
        "input_usd_per_million": "1.00",
        "output_usd_per_million": "2.00",
        "price_version": "fixture-price-v1",
    }
    return {
        "campaign_id": "fixture-campaign",
        "application_id": "test-application",
        "isolation": {"database_disposable": True, "comparison_disabled": True},
        "cases": [
            {
                "case_id": "sdk-chat",
                "surface": "openai_sdk",
                "n": 1,
                "retry_count": 0,
                "roles": {"generation": role},
            }
        ],
    }


def test_dispatch_budget_uses_eighty_percent_and_exact_decimal_arithmetic():
    budget = BudgetLedger(Decimal("0.20"), max_generation_requests=8)
    first = budget.reserve_request(Decimal("0.10"))
    second = budget.reserve_request(Decimal("0.06"))

    with pytest.raises(BudgetExceededError):
        budget.reserve_request(Decimal("0.0001"))

    budget.settle_request(first, Decimal("0.04"))
    third = budget.reserve_request(Decimal("0.04"))
    budget.settle_request(second, Decimal("0.06"))
    budget.settle_request(third, Decimal("0.02"))
    assert budget.settled_usd == Decimal("0.12")
    assert budget.dispatch_limit_usd == Decimal("0.16")
    assert budget.reserve_usd == Decimal("0.04")


def test_parallel_reservations_cannot_exceed_dispatch_ceiling():
    budget = BudgetLedger(Decimal("0.20"), max_generation_requests=8)

    def reserve(_index: int):
        try:
            return budget.reserve_request(Decimal("0.02"))
        except BudgetExceededError:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        reservations = list(pool.map(reserve, range(12)))

    assert sum(reservation is not None for reservation in reservations) == 8
    assert budget.outstanding_usd == Decimal("0.16")


def test_unknown_usage_keeps_the_reservation_and_blocks_more_dispatch():
    budget = BudgetLedger(Decimal("0.20"), max_generation_requests=8)
    reservation = budget.reserve_request(Decimal("0.16"))
    budget.settle_request(reservation, None)

    with pytest.raises(BudgetExceededError):
        budget.reserve_request(Decimal("0.001"))

    assert budget.unknown_usd == Decimal("0.16")
    assert budget.outstanding_usd == Decimal("0.16")


def test_generation_request_limit_counts_expanded_manifest_roles():
    manifest = _manifest()
    manifest["cases"] = [
        {**manifest["cases"][0], "case_id": f"sdk-chat-{index}"}
        for index in range(9)
    ]

    with pytest.raises(ManifestError, match="generation request limit"):
        validate_manifest(manifest, max_cost_usd=Decimal("0.20"), max_generation_requests=8)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["cases"][0].update(retry_count=1), "retries must be zero"),
        (lambda value: value["cases"][0]["roles"]["generation"].pop("price_version"), "price_version"),
        (lambda value: value["cases"][0].update(body="must not be persisted"), "forbidden field"),
        (lambda value: value.update(applicationKey="secret"), "forbidden field"),
        (lambda value: value.update(access_token="secret"), "forbidden field"),
        (lambda value: value["isolation"].update(database_disposable=False), "disposable database"),
    ],
)
def test_manifest_rejects_unbounded_or_sensitive_campaigns(mutate, message):
    manifest = _manifest()
    mutate(manifest)

    with pytest.raises(ManifestError, match=message):
        validate_manifest(manifest, max_cost_usd=Decimal("0.20"), max_generation_requests=8)


def test_cli_rejects_caps_above_user_ceiling(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")

    assert main(["--campaign-manifest", str(path), "--dry-run", "--max-cost-usd", "0.21"]) == 2


def test_dry_run_writes_secret_free_preflight_without_network(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    output_dir = tmp_path / "evidence"
    monkeypatch.setenv("LATENCY_SMOKE_APPLICATION_KEY", "must-not-be-read-or-printed")
    monkeypatch.setenv("LATENCY_SMOKE_USER_TOKEN", "must-not-be-read-or-printed")

    assert main([
        "--campaign-manifest", str(manifest_path),
        "--application-id", "test-application",
        "--dry-run",
        "--output-dir", str(output_dir),
    ]) == 0

    report = (output_dir / "preflight.json").read_text(encoding="utf-8")
    assert "fixture-campaign" in report
    assert "must-not-be-read-or-printed" not in report
    assert '"network_requests": 0' in report

