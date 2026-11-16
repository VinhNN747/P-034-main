"""Import the Anthropic provider catalog without a credential or inference call.

Run without arguments for a read-only preview, pass ``--apply`` to insert
missing rows, or pass ``--restore <receipt>`` to remove unchanged rows created
by one import. User-edited or referenced rows are retained.
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared.database.session import create_engine_from_settings
from src.shared.dtos.model_features import ModelFeatureMetadata
from src.shared.models.core import utc_now
from src.shared.models.gateway import ApplicationGatewayConfig, Model, ModelPoolMember, Provider
from src.shared.models.latency import LatencyObservationRecord
from src.shared.models.provider_credentials import ProviderCredential, ServiceProviderBinding

MANIFEST_PATH = PROJECT_ROOT / "src" / "scripts" / "catalogs" / "anthropic-2026-10-17.json"
MODEL_FINGERPRINT_FIELDS = (
    "id", "provider_id", "name", "display_name", "status", "capabilities_json", "context_window",
    "max_output_tokens", "feature_metadata_json", "target_latency_ms", "input_price_per_million",
    "output_price_per_million", "pricing_version", "quality_tier", "health_status", "last_probe_at",
    "last_probe_latency_ms", "last_probe_error", "last_probe_credential_version", "created_at", "updated_at",
)
PROVIDER_FINGERPRINT_FIELDS = (
    "id", "name", "display_name", "kind", "purpose", "status", "base_url", "secret_env_var",
    "owner_user_id", "created_at", "updated_at",
)


@dataclass
class ImportSummary:
    provider_id: str | None = None
    created_provider_id: str | None = None
    created_model_ids: dict[str, str] = field(default_factory=dict)
    models_to_create: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    receipt: dict[str, Any] | None = None
    deleted_provider_id: str | None = None
    deleted_model_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_manifest(path: Path = MANIFEST_PATH) -> tuple[dict, list[dict]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    common = raw["common_features"]
    models = []
    for item in raw["models"]:
        upstream = {**common["upstream_features"], **item.get("feature_overrides", {}).get("upstream_features", {})}
        gateway = {**common["gateway_features"], **item.get("feature_overrides", {}).get("gateway_features", {})}
        feature_metadata = ModelFeatureMetadata.model_validate({
            "schema_version": 1,
            "upstream_features": upstream,
            "gateway_features": gateway,
            **item["feature_metadata"],
            "provenance": raw["provenance"],
        })
        models.append({
            **item,
            "feature_metadata": feature_metadata.model_dump(mode="json"),
            "capabilities": raw["capabilities"],
            "context_window": raw["context_window"],
            "max_output_tokens": raw["max_output_tokens"],
            "pricing_version": raw["pricing_version"],
        })
    return raw["provider"], models


def _fingerprint(row: Any, fields: tuple[str, ...]) -> str:
    values = {}
    for name in fields:
        value = getattr(row, name)
        if isinstance(value, datetime):
            value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
            value = value.isoformat(timespec="microseconds")
        elif name in {"input_price_per_million", "output_price_per_million"} and value is not None:
            value = float(value)
        values[name] = value
    normalized = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def _rows(session: AsyncSession, statement) -> list[Any]:
    result = await session.execute(statement)
    return list(result.scalars().all())


async def import_catalog(
    session: AsyncSession,
    *,
    apply: bool,
    manifest_path: Path = MANIFEST_PATH,
) -> ImportSummary:
    provider_values, model_values = _read_manifest(manifest_path)
    summary = ImportSummary(models_to_create=[item["name"] for item in model_values])
    matching_providers = await _rows(session, select(Provider).where(Provider.name == provider_values["name"]))
    if len(matching_providers) > 1:
        summary.conflicts.append("Multiple providers named 'anthropic' exist.")
    provider = matching_providers[0] if len(matching_providers) == 1 else None
    if provider is not None:
        summary.skipped.append("provider:anthropic")
    if provider is not None and (
        provider.kind != "anthropic"
        or provider.purpose != "target"
        or provider.base_url != "https://api.anthropic.com"
        or provider.owner_user_id is not None
    ):
        summary.conflicts.append("Provider 'anthropic' exists with a different kind, purpose, endpoint or owner.")

    provider_id = provider.id if provider is not None else str(uuid4())
    existing_models: dict[str, Model] = {}
    for values in model_values:
        matches = await _rows(
            session,
            select(Model).where(Model.provider_id == provider_id, Model.name == values["name"]),
        )
        if len(matches) > 1:
            summary.conflicts.append(f"Multiple models named '{values['name']}' exist for provider 'anthropic'.")
        elif matches:
            existing_models[values["name"]] = matches[0]
            summary.skipped.append(f"model:{values['name']}")

    if summary.conflicts:
        if apply:
            await session.rollback()
        summary.models_to_create = []
        return summary

    models_to_create = [item for item in model_values if item["name"] not in existing_models]
    summary.models_to_create = [item["name"] for item in models_to_create]
    if not apply:
        return summary

    now = utc_now()
    created_provider = None
    created_models = []
    try:
        if provider is None:
            created_provider = Provider(
                id=provider_id,
                **provider_values,
                created_at=now,
                updated_at=now,
            )
            session.add(created_provider)
            provider = created_provider
            summary.created_provider_id = provider_id
            await session.flush()
        summary.provider_id = provider.id

        for values in models_to_create:
            model = Model(
                id=str(uuid4()),
                provider_id=provider.id,
                name=values["name"],
                display_name=values["display_name"],
                status="inactive",
                capabilities_json=values["capabilities"],
                context_window=values["context_window"],
                max_output_tokens=values["max_output_tokens"],
                feature_metadata_json=values["feature_metadata"],
                target_latency_ms=None,
                input_price_per_million=values["input_price_per_million"],
                output_price_per_million=values["output_price_per_million"],
                pricing_version=values["pricing_version"],
                quality_tier=values["quality_tier"],
                health_status="unknown",
                last_probe_at=None,
                last_probe_latency_ms=None,
                last_probe_error=None,
                last_probe_credential_version=None,
                created_at=now,
                updated_at=now,
            )
            session.add(model)
            created_models.append(model)
            summary.created_model_ids[model.name] = model.id

        await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        summary.conflicts.append("Catalog changed during import; no rows were committed. Re-run the preview.")
        summary.created_provider_id = None
        summary.created_model_ids.clear()
        summary.provider_id = None
        return summary

    summary.receipt = {
        "schema_version": 1,
        "created_at": now.isoformat(),
        "provider": {
            "id": provider.id,
            "created": created_provider is not None,
            "fingerprint": _fingerprint(provider, PROVIDER_FINGERPRINT_FIELDS),
        },
        "models": [
            {"id": model.id, "name": model.name, "fingerprint": _fingerprint(model, MODEL_FINGERPRINT_FIELDS)}
            for model in created_models
        ],
    }
    return summary


async def _model_reference_conflicts(session: AsyncSession, model_ids: set[str]) -> list[str]:
    conflicts = []
    if not model_ids:
        return conflicts
    pool_count = await session.scalar(
        select(func.count()).select_from(ModelPoolMember).where(ModelPoolMember.model_id.in_(model_ids))
    )
    if pool_count:
        conflicts.append("One or more imported models are referenced by a model pool.")
    fallback_count = await session.scalar(
        select(func.count()).select_from(ApplicationGatewayConfig).where(
            ApplicationGatewayConfig.fallback_model_id.in_(model_ids)
        )
    )
    if fallback_count:
        conflicts.append("One or more imported models are configured as an application fallback.")
    app_configs = await _rows(session, select(ApplicationGatewayConfig))
    if any(model_id in (row.fallback_model_ids_json or []) for row in app_configs for model_id in model_ids):
        conflicts.append("One or more imported models are listed in an application fallback chain.")
    observations = await session.scalar(
        select(func.count()).select_from(LatencyObservationRecord).where(
            LatencyObservationRecord.catalog_model_id.in_(model_ids)
        )
    )
    if observations:
        conflicts.append("One or more imported models have latency observations.")
    return conflicts


async def restore_catalog(session: AsyncSession, receipt: dict[str, Any]) -> ImportSummary:
    summary = ImportSummary()
    if receipt.get("schema_version") != 1:
        summary.conflicts.append("Unsupported restore receipt version.")
        return summary

    model_receipts = receipt.get("models", [])
    model_ids = {item.get("id") for item in model_receipts if item.get("id")}
    models = await _rows(session, select(Model).where(Model.id.in_(model_ids))) if model_ids else []
    model_by_id = {row.id: row for row in models}
    for item in model_receipts:
        row = model_by_id.get(item.get("id"))
        if row is not None and _fingerprint(row, MODEL_FINGERPRINT_FIELDS) != item.get("fingerprint"):
            summary.conflicts.append(f"Model '{item.get('name')}' changed after import; restore refused.")
    summary.conflicts.extend(await _model_reference_conflicts(session, set(model_by_id)))

    provider_receipt = receipt.get("provider")
    provider = None
    if provider_receipt:
        provider = await session.scalar(select(Provider).where(Provider.id == provider_receipt.get("id")))
        if provider is not None and _fingerprint(provider, PROVIDER_FINGERPRINT_FIELDS) != provider_receipt.get("fingerprint"):
            summary.conflicts.append("Provider 'anthropic' changed after import; restore refused.")
        if provider is not None:
            credential = await session.scalar(
                select(ProviderCredential).where(ProviderCredential.provider_id == provider.id)
            )
            if credential is not None:
                summary.conflicts.append("Provider has a credential; restore will not remove it.")
            binding = await session.scalar(
                select(ServiceProviderBinding).where(ServiceProviderBinding.provider_id == provider.id)
            )
            if binding is not None:
                summary.conflicts.append("Provider is used by an internal service binding; restore refused.")
            remaining_models = await _rows(
                session,
                select(Model).where(Model.provider_id == provider.id, Model.id.not_in(model_ids)),
            )
            if remaining_models:
                summary.conflicts.append("Provider has models outside this receipt; restore refused.")

    if summary.conflicts:
        await session.rollback()
        return summary

    for model in models:
        await session.delete(model)
        summary.deleted_model_ids.append(model.id)
    if provider is not None and provider_receipt.get("created", False):
        await session.delete(provider)
        summary.deleted_provider_id = provider.id
    await session.commit()
    return summary


async def _run(args: argparse.Namespace) -> int:
    engine = create_engine_from_settings(None)
    try:
        async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
            if args.restore is not None:
                receipt = json.loads(args.restore.read_text(encoding="utf-8"))
                summary = await restore_catalog(session, receipt)
            else:
                summary = await import_catalog(session, apply=args.apply)
                if args.apply and summary.receipt and (summary.created_provider_id or summary.created_model_ids):
                    receipt_dir = PROJECT_ROOT / "tmp"
                    receipt_dir.mkdir(exist_ok=True)
                    path = receipt_dir / f"anthropic-import-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
                    path.write_text(json.dumps(summary.receipt, indent=2), encoding="utf-8")
                    print(f"Receipt: {path}")
    finally:
        await engine.dispose()
    print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
    return 2 if summary.conflicts else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--apply", action="store_true", help="commit missing provider and model rows")
    actions.add_argument("--restore", type=Path, help="restore only rows owned by a receipt")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
