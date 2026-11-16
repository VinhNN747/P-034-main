"""Safely merge the OpenAI Live Test provider into the canonical OpenAI row."""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models import (
    ApplicationGatewayConfig,
    BillingTransactionRecord,
    GatewayRequestRecord,
    Model,
    ModelExecutionRecord,
    ModelPoolMember,
    Provider,
    ProviderCredential,
    RoutingPolicy,
    RuntimeOperationRecord,
    ServiceProviderBinding,
    UsageEventRecord,
)
from src.shared.services.credential_crypto import CredentialCrypto

JSONValue = Any

EXPECTED_LIVE_MODEL_NAMES = frozenset({"gpt-4o-mini", "gpt-5.6-luna", "gpt-5.6-terra", "text-embedding-3-small"})
COLLIDING_MODEL_NAMES = frozenset({"gpt-4o-mini", "gpt-5.6-luna"})
MOVABLE_MODEL_NAMES = frozenset({"gpt-5.6-terra", "text-embedding-3-small"})
APPROVED_ROUTING_POLICY_NAME = "policy-live-20260928103943"


class MergePreconditionError(RuntimeError):
    """A secret-safe, stable precondition failure for this one-shot migration."""

    def __init__(self, reason: str, *, context: str = "") -> None:
        message = f"merge_precondition:{reason}"
        if context:
            message = f"{message} ({context})"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PolicyFallbackRewrite:
    policy_id: str
    policy_name: str
    policy_status: str
    old_fallback_chain: tuple[str, ...]
    duplicate_model_id: str
    canonical_model_id: str


@dataclass(frozen=True, slots=True)
class MergeInventory:
    canonical_provider_id: str
    duplicate_provider_id: str
    canonical_provider_name: str
    duplicate_provider_name: str
    canonical_display_name: str
    duplicate_display_name: str
    canonical_status: str
    duplicate_status: str
    model_ids_to_move: tuple[str, ...]
    model_ids_to_delete: tuple[str, ...]
    model_names_to_move: tuple[str, ...]
    model_names_to_delete: tuple[str, ...]
    binding_purposes: tuple[str, ...]
    routing_policy_fallback_rewrites: tuple[PolicyFallbackRewrite, ...]
    row_counts: tuple[tuple[str, int], ...]
    affected_row_counts: tuple[tuple[str, int], ...]
    reference_counts: tuple[tuple[str, int], ...]
    fingerprint: str


def rewrite_provider_fields(
    value: JSONValue,
    *,
    old_provider_id: str,
    old_provider_name: str,
    canonical_provider_id: str,
    canonical_provider_name: str,
) -> tuple[JSONValue, int]:
    """Copy JSON while replacing only recognized structured provider fields."""
    replacements_by_field = {
        "provider_id": {old_provider_id: canonical_provider_id},
        "provider_name": {old_provider_name: canonical_provider_name},
        "embedding_provider": {old_provider_name: canonical_provider_name},
        "allowed_providers": {
            old_provider_id: canonical_provider_id,
            old_provider_name: canonical_provider_name,
        },
        "providers_used": {old_provider_name: canonical_provider_name},
    }
    replacements = 0

    def visit(node: JSONValue, parent_key: str | None = None) -> JSONValue:
        nonlocal replacements

        if isinstance(node, dict):
            return {key: visit(child, key) for key, child in node.items()}
        if isinstance(node, list):
            return [visit(item, parent_key) for item in node]
        if isinstance(node, str) and parent_key in replacements_by_field:
            replacement = replacements_by_field[parent_key].get(node)
            if replacement is not None:
                replacements += 1
                return replacement
        return node

    return visit(value), replacements


def _raise_precondition(reason: str, **counts: int) -> None:
    context = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    raise MergePreconditionError(reason, context=context)


async def _count_rows(session: AsyncSession, model: type) -> int:
    value = await session.scalar(select(func.count()).select_from(model))
    return int(value or 0)


async def _count_scalar_provider_rows(session: AsyncSession, model: type, column: Any, old_provider_name: str) -> int:
    value = await session.scalar(select(func.count()).select_from(model).where(column == old_provider_name))
    return int(value or 0)


async def _count_json_rows_with_changes(
    session: AsyncSession,
    model: type,
    columns: tuple[Any, ...],
    *,
    old_provider_id: str,
    old_provider_name: str,
    canonical_provider_id: str,
    canonical_provider_name: str,
) -> int:
    rows = (await session.execute(select(*columns))).all()
    changed = 0
    for row in rows:
        if any(
            rewrite_provider_fields(
                value,
                old_provider_id=old_provider_id,
                old_provider_name=old_provider_name,
                canonical_provider_id=canonical_provider_id,
                canonical_provider_name=canonical_provider_name,
            )[1]
            for value in row
        ):
            changed += 1
    return changed


async def _count_model_fallback_references(
    session: AsyncSession, duplicate_model_ids: set[str], *, lock_rows: bool = False
) -> tuple[int, int, list[tuple[RoutingPolicy, str]]]:
    config_query = select(
        ApplicationGatewayConfig.fallback_model_id,
        ApplicationGatewayConfig.fallback_model_ids_json,
    )
    policy_query = select(RoutingPolicy).order_by(RoutingPolicy.id)
    if lock_rows:
        config_query = config_query.with_for_update()
        policy_query = policy_query.with_for_update()
    configs = (await session.execute(config_query)).all()
    direct_fallbacks = sum(1 for model_id, _ in configs if model_id in duplicate_model_ids)
    json_fallbacks = 0
    for _, model_ids in configs:
        if isinstance(model_ids, list):
            json_fallbacks += sum(1 for model_id in model_ids if model_id in duplicate_model_ids)

    policies = (await session.scalars(policy_query)).all()
    routing_policy_hits = []
    for policy in policies:
        model_ids = policy.fallback_chain_json
        if isinstance(model_ids, list):
            routing_policy_hits.extend((policy, model_id) for model_id in model_ids if model_id in duplicate_model_ids)
    return direct_fallbacks, json_fallbacks, routing_policy_hits


async def inspect_merge(
    session: AsyncSession,
    crypto: CredentialCrypto | None,
    *,
    lock_rows: bool = False,
) -> MergeInventory:
    """Build a read-only, secret-safe inventory and reject unapproved drift."""
    provider_query = select(Provider).order_by(Provider.id)
    if lock_rows:
        provider_query = provider_query.with_for_update()
    providers = list((await session.scalars(provider_query)).all())
    canonical_candidates = [row for row in providers if row.name == "openai" or row.display_name == "OpenAI"]
    duplicate_candidates = [
        row for row in providers if row.name.startswith("openai-live-") or row.display_name == "OpenAI Live Test"
    ]
    if len(canonical_candidates) != 1 or len(duplicate_candidates) != 1:
        _raise_precondition(
            "provider_missing_or_ambiguous",
            canonical_candidates=len(canonical_candidates),
            duplicate_candidates=len(duplicate_candidates),
        )
    canonical, duplicate = canonical_candidates[0], duplicate_candidates[0]
    if (
        canonical.name != "openai"
        or canonical.display_name != "OpenAI"
        or not duplicate.name.startswith("openai-live-")
        or duplicate.display_name != "OpenAI Live Test"
        or canonical.kind != "openai"
        or duplicate.kind != "openai"
        or canonical.purpose != "target"
        or duplicate.purpose != "target"
        or canonical.status != "active"
        or duplicate.status != "active"
    ):
        _raise_precondition("provider_identity_or_status_invalid", providers=2)

    model_query = (
        select(Model)
        .where(Model.provider_id.in_((canonical.id, duplicate.id)))
        .order_by(Model.provider_id, Model.name, Model.id)
    )
    if lock_rows:
        model_query = model_query.with_for_update()
    models = list((await session.scalars(model_query)).all())
    canonical_models = [row for row in models if row.provider_id == canonical.id]
    duplicate_models = [row for row in models if row.provider_id == duplicate.id]
    duplicate_names = [row.name for row in duplicate_models]
    canonical_names = [row.name for row in canonical_models]
    if set(duplicate_names) != EXPECTED_LIVE_MODEL_NAMES or len(duplicate_names) != len(EXPECTED_LIVE_MODEL_NAMES):
        _raise_precondition("live_model_catalog_drift", live_models=len(duplicate_models))
    for name in COLLIDING_MODEL_NAMES:
        if canonical_names.count(name) != 1 or duplicate_names.count(name) != 1:
            _raise_precondition("model_collision_drift", models=len(models))
    for name in MOVABLE_MODEL_NAMES:
        if duplicate_names.count(name) != 1 or canonical_names.count(name) != 0:
            _raise_precondition("movable_model_conflict", models=len(models))

    moving_models = sorted(
        (row for row in duplicate_models if row.name in MOVABLE_MODEL_NAMES),
        key=lambda row: row.name,
    )
    deleting_models = sorted(
        (row for row in duplicate_models if row.name in COLLIDING_MODEL_NAMES),
        key=lambda row: row.name,
    )
    deleting_ids = {row.id for row in deleting_models}

    binding_query = select(ServiceProviderBinding).where(
        or_(
            ServiceProviderBinding.provider_id == duplicate.id,
            ServiceProviderBinding.purpose == "internal_openai",
        )
    )
    if lock_rows:
        binding_query = binding_query.with_for_update()
    bindings = list((await session.scalars(binding_query)).all())
    if len(bindings) != 1 or bindings[0].purpose != "internal_openai" or bindings[0].provider_id != duplicate.id:
        _raise_precondition("binding_drift", bindings=len(bindings))

    credential_query = select(ProviderCredential).where(
        ProviderCredential.provider_id.in_((canonical.id, duplicate.id))
    )
    if lock_rows:
        credential_query = credential_query.with_for_update()
    credentials = list((await session.scalars(credential_query)).all())
    credentials_by_provider = {row.provider_id: row for row in credentials}
    if len(credentials) != 2 or set(credentials_by_provider) != {canonical.id, duplicate.id}:
        _raise_precondition("credential_missing_or_ambiguous", provider_credentials=len(credentials))
    if any(row.status != "active" for row in credentials):
        _raise_precondition("credential_inactive", provider_credentials=len(credentials))
    if crypto is None:
        _raise_precondition("credential_crypto_unavailable", provider_credentials=len(credentials))
    try:
        plaintexts = {
            provider_id: crypto.decrypt(
                provider_id=credential.provider_id,
                credential_id=credential.id,
                version=credential.version,
                ciphertext=credential.encrypted_api_key,
            )
            for provider_id, credential in credentials_by_provider.items()
        }
    except Exception:
        _raise_precondition("credential_decryption_failed", provider_credentials=len(credentials))
    if not hmac.compare_digest(plaintexts[canonical.id], plaintexts[duplicate.id]):
        _raise_precondition("credential_values_differ", provider_credentials=len(credentials))

    member_query = select(ModelPoolMember).where(ModelPoolMember.model_id.in_(deleting_ids))
    if lock_rows:
        member_query = member_query.with_for_update()
    pool_members = list((await session.scalars(member_query)).all()) if deleting_ids else []
    direct_fallbacks, json_fallbacks, routing_policy_hits = await _count_model_fallback_references(
        session, deleting_ids, lock_rows=lock_rows
    )
    live_gpt4_id = next(row.id for row in deleting_models if row.name == "gpt-4o-mini")
    canonical_gpt4 = [row for row in canonical_models if row.name == "gpt-4o-mini"]
    routing_policy_fallback_rewrites: tuple[PolicyFallbackRewrite, ...] = ()
    if routing_policy_hits:
        if (
            len(routing_policy_hits) == 1
            and routing_policy_hits[0][0].name == APPROVED_ROUTING_POLICY_NAME
            and routing_policy_hits[0][0].status == "active"
            and routing_policy_hits[0][1] == live_gpt4_id
            and len(canonical_gpt4) == 1
            and isinstance(routing_policy_hits[0][0].fallback_chain_json, list)
            and routing_policy_hits[0][0].fallback_chain_json.count(live_gpt4_id) == 1
        ):
            policy, _ = routing_policy_hits[0]
            routing_policy_fallback_rewrites = (
                PolicyFallbackRewrite(
                    policy_id=policy.id,
                    policy_name=policy.name,
                    policy_status=policy.status,
                    old_fallback_chain=tuple(policy.fallback_chain_json),
                    duplicate_model_id=live_gpt4_id,
                    canonical_model_id=canonical_gpt4[0].id,
                ),
            )
    reference_counts = (
        ("duplicate_model_pool_members", len(pool_members)),
        ("duplicate_model_fallback_model_id", direct_fallbacks),
        ("duplicate_model_fallback_model_ids_json", json_fallbacks),
        ("duplicate_model_routing_policy_fallbacks", len(routing_policy_hits)),
        (
            "terra_pool_members",
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(ModelPoolMember)
                    .where(
                        ModelPoolMember.model_id == next(row.id for row in moving_models if row.name == "gpt-5.6-terra")
                    )
                )
                or 0
            ),
        ),
    )
    unapproved_policy_fallbacks = len(routing_policy_hits) - len(routing_policy_fallback_rewrites)
    if pool_members or direct_fallbacks or json_fallbacks or unapproved_policy_fallbacks:
        _raise_precondition(
            "unexpected_model_reference",
            pool_members=len(pool_members),
            fallback_refs=direct_fallbacks + json_fallbacks + unapproved_policy_fallbacks,
        )

    affected_row_counts = (
        (
            "gateway_requests.strategies_json",
            await _count_json_rows_with_changes(
                session,
                GatewayRequestRecord,
                (GatewayRequestRecord.strategies_json,),
                old_provider_id=duplicate.id,
                old_provider_name=duplicate.name,
                canonical_provider_id=canonical.id,
                canonical_provider_name=canonical.name,
            ),
        ),
        (
            "gateway_requests.response_json",
            await _count_json_rows_with_changes(
                session,
                GatewayRequestRecord,
                (GatewayRequestRecord.response_json,),
                old_provider_id=duplicate.id,
                old_provider_name=duplicate.name,
                canonical_provider_id=canonical.id,
                canonical_provider_name=canonical.name,
            ),
        ),
        (
            "runtime_operations.config_snapshot_json",
            await _count_json_rows_with_changes(
                session,
                RuntimeOperationRecord,
                (RuntimeOperationRecord.config_snapshot_json,),
                old_provider_id=duplicate.id,
                old_provider_name=duplicate.name,
                canonical_provider_id=canonical.id,
                canonical_provider_name=canonical.name,
            ),
        ),
        (
            "runtime_operations.public_result_json",
            await _count_json_rows_with_changes(
                session,
                RuntimeOperationRecord,
                (RuntimeOperationRecord.public_result_json,),
                old_provider_id=duplicate.id,
                old_provider_name=duplicate.name,
                canonical_provider_id=canonical.id,
                canonical_provider_name=canonical.name,
            ),
        ),
        (
            "model_executions.provider",
            await _count_scalar_provider_rows(
                session, ModelExecutionRecord, ModelExecutionRecord.provider, duplicate.name
            ),
        ),
        (
            "usage_events.provider",
            await _count_scalar_provider_rows(session, UsageEventRecord, UsageEventRecord.provider, duplicate.name),
        ),
    )
    row_models = (
        ("providers", Provider),
        ("models", Model),
        ("provider_credentials", ProviderCredential),
        ("service_provider_bindings", ServiceProviderBinding),
        ("model_pool_members", ModelPoolMember),
        ("application_gateway_configs", ApplicationGatewayConfig),
        ("routing_policies", RoutingPolicy),
        ("gateway_requests", GatewayRequestRecord),
        ("runtime_operations", RuntimeOperationRecord),
        ("model_executions", ModelExecutionRecord),
        ("usage_events", UsageEventRecord),
        ("billing_transactions", BillingTransactionRecord),
    )
    row_count_values = []
    for name, model in row_models:
        row_count_values.append((name, await _count_rows(session, model)))
    row_counts = tuple(row_count_values)
    credential_fingerprint = sorted(
        (
            row.id,
            row.provider_id,
            row.status,
            row.version,
            hashlib.sha256(row.encrypted_api_key.encode("utf-8")).hexdigest(),
        )
        for row in credentials
    )
    model_fingerprint = sorted(
        (
            row.id,
            row.provider_id,
            row.name,
            row.display_name,
            row.status,
            row.capabilities_json,
            row.target_latency_ms,
            row.input_price_per_million,
            row.output_price_per_million,
            row.pricing_version,
        )
        for row in models
    )
    fingerprint_data = {
        "providers": [
            (row.id, row.name, row.display_name, row.kind, row.purpose, row.status) for row in (canonical, duplicate)
        ],
        "models": model_fingerprint,
        "credentials": credential_fingerprint,
        "bindings": sorted((row.purpose, row.provider_id) for row in bindings),
        "routing_policy_fallback_rewrites": [
            (
                rewrite.policy_id,
                rewrite.policy_name,
                rewrite.policy_status,
                rewrite.old_fallback_chain,
                rewrite.duplicate_model_id,
                rewrite.canonical_model_id,
            )
            for rewrite in routing_policy_fallback_rewrites
        ],
        "row_counts": row_counts,
        "affected_row_counts": affected_row_counts,
        "reference_counts": reference_counts,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return MergeInventory(
        canonical_provider_id=canonical.id,
        duplicate_provider_id=duplicate.id,
        canonical_provider_name=canonical.name,
        duplicate_provider_name=duplicate.name,
        canonical_display_name=canonical.display_name,
        duplicate_display_name=duplicate.display_name,
        canonical_status=canonical.status,
        duplicate_status=duplicate.status,
        model_ids_to_move=tuple(row.id for row in moving_models),
        model_ids_to_delete=tuple(row.id for row in deleting_models),
        model_names_to_move=tuple(row.name for row in moving_models),
        model_names_to_delete=tuple(row.name for row in deleting_models),
        binding_purposes=tuple(row.purpose for row in bindings),
        routing_policy_fallback_rewrites=routing_policy_fallback_rewrites,
        row_counts=row_counts,
        affected_row_counts=affected_row_counts,
        reference_counts=reference_counts,
        fingerprint=fingerprint,
    )


def render_inventory(inventory: MergeInventory) -> str:
    """Render only approved labels, row counts, statuses and the hash fingerprint."""
    rows = [
        "OpenAI provider merge preflight",
        f"Providers: {inventory.canonical_display_name} ({inventory.canonical_status}), "
        f"{inventory.duplicate_display_name} ({inventory.duplicate_status})",
        "Models to move: " + ", ".join(inventory.model_names_to_move),
        "Duplicate models to delete: " + ", ".join(inventory.model_names_to_delete),
        "Bindings to rebind: " + ", ".join(inventory.binding_purposes),
        "Routing policy fallback references to remap: "
        + ", ".join(rewrite.policy_name for rewrite in inventory.routing_policy_fallback_rewrites),
        "Affected rows: " + ", ".join(f"{name}={count}" for name, count in inventory.affected_row_counts),
        "Current row counts: " + ", ".join(f"{name}={count}" for name, count in inventory.row_counts),
        f"Inventory fingerprint: {inventory.fingerprint}",
    ]
    return "\n".join(rows)


def _quote_libpq_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _backup_conninfo(database_url: URL) -> str:
    if database_url.drivername.split("+", 1)[0] != "postgresql" or not database_url.database:
        _raise_precondition("backup_database_type_unsupported")
    parts: list[tuple[str, str]] = []
    for key, value in (
        ("host", database_url.host),
        ("port", str(database_url.port) if database_url.port else None),
        ("dbname", database_url.database),
        ("user", database_url.username),
    ):
        if value:
            parts.append((key, value))
    supported_query_options = {
        "application_name",
        "channel_binding",
        "connect_timeout",
        "gssencmode",
        "options",
        "sslcert",
        "sslkey",
        "sslmode",
        "sslrootcert",
        "target_session_attrs",
    }
    for key, value in sorted(database_url.query.items()):
        if key not in supported_query_options:
            continue
        values = value if isinstance(value, tuple) else (value,)
        for item in values:
            parts.append((key, str(item)))
    return " ".join(f"{key}={_quote_libpq_value(value)}" for key, value in parts)


def create_verified_backup(database_url: URL, destination: Path) -> None:
    """Create and validate a private custom-format dump without exposing DB secrets."""
    if destination.is_symlink():
        _raise_precondition("backup_path_must_be_new")
    destination_path = destination.expanduser().resolve()
    repo_root = PROJECT_ROOT.resolve()
    try:
        destination_path.relative_to(repo_root)
    except ValueError:
        pass
    else:
        _raise_precondition("backup_path_inside_repository")
    if destination.exists() or destination_path.exists():
        _raise_precondition("backup_path_must_be_new")

    conninfo = _backup_conninfo(database_url)
    try:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor = os.open(
            destination_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        os.close(file_descriptor)
    except Exception:
        _raise_precondition("backup_path_unavailable")

    child_environment = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"}
    if database_url.password is not None:
        child_environment["PGPASSWORD"] = database_url.password
    try:
        dump_result = subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                "--no-password",
                "--file",
                str(destination_path),
                "--dbname",
                conninfo,
            ],
            env=child_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if dump_result.returncode != 0 or not destination_path.is_file() or destination_path.stat().st_size == 0:
            _raise_precondition("backup_dump_failed")
        restore_result = subprocess.run(
            ["pg_restore", "--list", str(destination_path)],
            env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"},
            capture_output=True,
            text=True,
            check=False,
        )
        if restore_result.returncode != 0:
            _raise_precondition("backup_validation_failed")
        destination_path.chmod(0o600)
    except MergePreconditionError:
        destination_path.unlink(missing_ok=True)
        raise
    except Exception:
        destination_path.unlink(missing_ok=True)
        _raise_precondition("backup_tool_failed")


async def _rewrite_json_column(
    session: AsyncSession,
    model: type,
    primary_key: Any,
    json_column: Any,
    *,
    duplicate: Provider,
    canonical: Provider,
) -> int:
    rows = (await session.execute(select(primary_key, json_column))).all()
    candidate_ids = []
    for row_id, value in rows:
        _, replacements = rewrite_provider_fields(
            value,
            old_provider_id=duplicate.id,
            old_provider_name=duplicate.name,
            canonical_provider_id=canonical.id,
            canonical_provider_name=canonical.name,
        )
        if replacements:
            candidate_ids.append(row_id)
    if not candidate_ids:
        return 0

    locked_rows = (
        await session.execute(select(primary_key, json_column).where(primary_key.in_(candidate_ids)).with_for_update())
    ).all()
    changed_rows = 0
    for row_id, value in locked_rows:
        rewritten, replacements = rewrite_provider_fields(
            value,
            old_provider_id=duplicate.id,
            old_provider_name=duplicate.name,
            canonical_provider_id=canonical.id,
            canonical_provider_name=canonical.name,
        )
        if not replacements:
            continue
        await session.execute(update(model).where(primary_key == row_id).values({json_column.key: rewritten}))
        changed_rows += 1
    return changed_rows


def _model_catalog_snapshot(rows: list[Model]) -> dict[str, tuple[Any, ...]]:
    return {
        row.id: (
            row.provider_id,
            row.name,
            row.display_name,
            row.status,
            row.capabilities_json,
            row.context_window,
            row.target_latency_ms,
            row.input_price_per_million,
            row.output_price_per_million,
            row.pricing_version,
            row.quality_tier,
            row.health_status,
            row.last_probe_at,
            row.last_probe_latency_ms,
            row.last_probe_error,
            row.last_probe_credential_version,
        )
        for row in rows
    }


async def apply_merge(
    session: AsyncSession,
    crypto: CredentialCrypto,
    *,
    expected_fingerprint: str,
) -> dict[str, int]:
    """Apply the merge inside the caller's transaction; never commit here."""
    if not session.in_transaction():
        _raise_precondition("transaction_required")
    if re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint) is None:
        _raise_precondition("inventory_fingerprint_invalid")

    session.expire_all()
    inventory = await inspect_merge(session, crypto, lock_rows=True)
    if inventory.fingerprint != expected_fingerprint:
        _raise_precondition("inventory_fingerprint_mismatch")

    before_counts = dict(inventory.row_counts)
    duplicate = Provider(
        id=inventory.duplicate_provider_id,
        name=inventory.duplicate_provider_name,
        display_name=inventory.duplicate_display_name,
        kind="openai",
        purpose="target",
        status=inventory.duplicate_status,
    )
    canonical = Provider(
        id=inventory.canonical_provider_id,
        name=inventory.canonical_provider_name,
        display_name=inventory.canonical_display_name,
        kind="openai",
        purpose="target",
        status=inventory.canonical_status,
    )
    credential = await session.scalar(
        select(ProviderCredential).where(ProviderCredential.provider_id == canonical.id).with_for_update()
    )
    if credential is None:
        _raise_precondition("canonical_credential_missing", provider_credentials=0)
    canonical_credential_before = (
        credential.id,
        credential.provider_id,
        credential.encrypted_api_key,
        credential.status,
        credential.version,
    )
    canonical_models_before = (
        await session.scalars(select(Model).where(Model.provider_id == canonical.id).with_for_update())
    ).all()
    canonical_model_snapshot = _model_catalog_snapshot(list(canonical_models_before))
    terra_model_id = next(
        model_id
        for model_name, model_id in zip(inventory.model_names_to_move, inventory.model_ids_to_move)
        if model_name == "gpt-5.6-terra"
    )
    terra_members_before = tuple(
        (
            await session.execute(
                select(
                    ModelPoolMember.id,
                    ModelPoolMember.pool_id,
                    ModelPoolMember.model_id,
                    ModelPoolMember.role,
                )
                .where(ModelPoolMember.model_id == terra_model_id)
                .with_for_update()
            )
        ).all()
    )

    binding_result = await session.execute(
        update(ServiceProviderBinding)
        .where(
            ServiceProviderBinding.purpose == "internal_openai",
            ServiceProviderBinding.provider_id == duplicate.id,
        )
        .values(provider_id=canonical.id)
    )
    moved_result = await session.execute(
        update(Model)
        .where(Model.id.in_(inventory.model_ids_to_move), Model.provider_id == duplicate.id)
        .values(provider_id=canonical.id)
    )
    changed_counts = {
        "bindings_rebound": int(binding_result.rowcount or 0),
        "models_moved": int(moved_result.rowcount or 0),
    }
    if changed_counts["bindings_rebound"] != 1 or changed_counts["models_moved"] != 2:
        _raise_precondition("provider_binding_or_model_move_count_mismatch")

    policy_fallbacks_remapped = 0
    for rewrite in inventory.routing_policy_fallback_rewrites:
        policy = await session.scalar(
            select(RoutingPolicy).where(RoutingPolicy.id == rewrite.policy_id).with_for_update()
        )
        if (
            policy is None
            or policy.name != rewrite.policy_name
            or policy.status != rewrite.policy_status
            or tuple(policy.fallback_chain_json or ()) != rewrite.old_fallback_chain
            or rewrite.old_fallback_chain.count(rewrite.duplicate_model_id) != 1
        ):
            _raise_precondition("routing_policy_fallback_drift", routing_policies=1)
        new_fallback_chain = [
            rewrite.canonical_model_id if model_id == rewrite.duplicate_model_id else model_id
            for model_id in rewrite.old_fallback_chain
        ]
        result = await session.execute(
            update(RoutingPolicy)
            .where(
                RoutingPolicy.id == rewrite.policy_id,
                RoutingPolicy.name == rewrite.policy_name,
                RoutingPolicy.status == rewrite.policy_status,
            )
            .values(fallback_chain_json=new_fallback_chain)
        )
        if int(result.rowcount or 0) != 1:
            _raise_precondition("routing_policy_fallback_update_count_mismatch", routing_policies=1)
        policy_fallbacks_remapped += 1
    changed_counts["routing_policy_fallbacks_remapped"] = policy_fallbacks_remapped

    json_changes = (
        (
            "gateway_requests.strategies_json",
            await _rewrite_json_column(
                session,
                GatewayRequestRecord,
                GatewayRequestRecord.request_id,
                GatewayRequestRecord.strategies_json,
                duplicate=duplicate,
                canonical=canonical,
            ),
        ),
        (
            "gateway_requests.response_json",
            await _rewrite_json_column(
                session,
                GatewayRequestRecord,
                GatewayRequestRecord.request_id,
                GatewayRequestRecord.response_json,
                duplicate=duplicate,
                canonical=canonical,
            ),
        ),
        (
            "runtime_operations.config_snapshot_json",
            await _rewrite_json_column(
                session,
                RuntimeOperationRecord,
                RuntimeOperationRecord.request_id,
                RuntimeOperationRecord.config_snapshot_json,
                duplicate=duplicate,
                canonical=canonical,
            ),
        ),
        (
            "runtime_operations.public_result_json",
            await _rewrite_json_column(
                session,
                RuntimeOperationRecord,
                RuntimeOperationRecord.request_id,
                RuntimeOperationRecord.public_result_json,
                duplicate=duplicate,
                canonical=canonical,
            ),
        ),
    )
    for name, count in json_changes:
        changed_counts[name] = count

    execution_result = await session.execute(
        update(ModelExecutionRecord)
        .where(ModelExecutionRecord.provider == duplicate.name)
        .values(provider=canonical.name)
    )
    usage_result = await session.execute(
        update(UsageEventRecord).where(UsageEventRecord.provider == duplicate.name).values(provider=canonical.name)
    )
    changed_counts["model_executions.provider"] = int(execution_result.rowcount or 0)
    changed_counts["usage_events.provider"] = int(usage_result.rowcount or 0)
    if any(changed_counts[name] != value for name, value in inventory.affected_row_counts):
        _raise_precondition("history_update_count_mismatch", affected_tables=len(inventory.affected_row_counts))

    delete_models = await session.execute(delete(Model).where(Model.id.in_(inventory.model_ids_to_delete)))
    changed_counts["models_deleted"] = int(delete_models.rowcount or 0)
    delete_credential = await session.execute(
        delete(ProviderCredential).where(ProviderCredential.provider_id == duplicate.id)
    )
    changed_counts["provider_credentials_deleted"] = int(delete_credential.rowcount or 0)
    delete_provider = await session.execute(delete(Provider).where(Provider.id == duplicate.id))
    changed_counts["providers_deleted"] = int(delete_provider.rowcount or 0)
    if (
        changed_counts["models_deleted"] != 2
        or changed_counts["provider_credentials_deleted"] != 1
        or changed_counts["providers_deleted"] != 1
    ):
        _raise_precondition("duplicate_row_delete_count_mismatch")

    session.expire_all()
    canonical_provider = await session.get(Provider, canonical.id)
    if canonical_provider is None or canonical_provider.status != "active":
        _raise_precondition("canonical_provider_postcondition_failed", providers=1)
    canonical_credential_after = await session.scalar(
        select(ProviderCredential).where(ProviderCredential.provider_id == canonical.id)
    )
    if (
        canonical_credential_after is None
        or (
            canonical_credential_after.id,
            canonical_credential_after.provider_id,
            canonical_credential_after.encrypted_api_key,
            canonical_credential_after.status,
            canonical_credential_after.version,
        )
        != canonical_credential_before
    ):
        _raise_precondition("canonical_credential_changed", provider_credentials=1)
    binding_after = await session.get(ServiceProviderBinding, "internal_openai")
    if binding_after is None or binding_after.provider_id != canonical.id:
        _raise_precondition("binding_postcondition_failed", bindings=1)
    if await session.get(Provider, duplicate.id) is not None:
        _raise_precondition("duplicate_provider_remains", providers=1)
    if await session.scalar(select(func.count()).select_from(Model).where(Model.provider_id == duplicate.id)):
        _raise_precondition("duplicate_model_provider_reference_remains", models=1)
    if await session.scalar(
        select(func.count()).select_from(ProviderCredential).where(ProviderCredential.provider_id == duplicate.id)
    ):
        _raise_precondition("duplicate_credential_reference_remains", provider_credentials=1)
    if await session.scalar(
        select(func.count())
        .select_from(ServiceProviderBinding)
        .where(ServiceProviderBinding.provider_id == duplicate.id)
    ):
        _raise_precondition("duplicate_binding_reference_remains", bindings=1)

    remaining_policy_fallbacks = 0
    for policy in (await session.scalars(select(RoutingPolicy))).all():
        if isinstance(policy.fallback_chain_json, list):
            remaining_policy_fallbacks += sum(
                1 for model_id in policy.fallback_chain_json if model_id in inventory.model_ids_to_delete
            )
    if remaining_policy_fallbacks:
        _raise_precondition("duplicate_model_policy_fallback_remains", fallback_refs=remaining_policy_fallbacks)

    terra_after = await session.get(Model, terra_model_id)
    if terra_after is None or terra_after.provider_id != canonical.id:
        _raise_precondition("terra_model_postcondition_failed", models=1)
    terra_members_after = tuple(
        (
            await session.execute(
                select(
                    ModelPoolMember.id,
                    ModelPoolMember.pool_id,
                    ModelPoolMember.model_id,
                    ModelPoolMember.role,
                ).where(ModelPoolMember.model_id == terra_model_id)
            )
        ).all()
    )
    if terra_members_after != terra_members_before:
        _raise_precondition("terra_pool_membership_changed", model_pool_members=len(terra_members_after))
    canonical_models_after = (
        await session.scalars(select(Model).where(Model.id.in_(canonical_model_snapshot.keys())))
    ).all()
    if _model_catalog_snapshot(list(canonical_models_after)) != canonical_model_snapshot:
        _raise_precondition("canonical_model_catalog_changed", models=len(canonical_model_snapshot))

    remaining_old_labels = (
        await _count_json_rows_with_changes(
            session,
            GatewayRequestRecord,
            (GatewayRequestRecord.strategies_json, GatewayRequestRecord.response_json),
            old_provider_id=duplicate.id,
            old_provider_name=duplicate.name,
            canonical_provider_id=canonical.id,
            canonical_provider_name=canonical.name,
        ),
        await _count_json_rows_with_changes(
            session,
            RuntimeOperationRecord,
            (RuntimeOperationRecord.config_snapshot_json, RuntimeOperationRecord.public_result_json),
            old_provider_id=duplicate.id,
            old_provider_name=duplicate.name,
            canonical_provider_id=canonical.id,
            canonical_provider_name=canonical.name,
        ),
        await _count_scalar_provider_rows(session, ModelExecutionRecord, ModelExecutionRecord.provider, duplicate.name),
        await _count_scalar_provider_rows(session, UsageEventRecord, UsageEventRecord.provider, duplicate.name),
    )
    if any(remaining_old_labels):
        _raise_precondition("old_provider_history_label_remains", history_tables=len(remaining_old_labels))

    actual_counts = {}
    for name, model in (
        ("providers", Provider),
        ("models", Model),
        ("provider_credentials", ProviderCredential),
        ("service_provider_bindings", ServiceProviderBinding),
        ("model_pool_members", ModelPoolMember),
        ("application_gateway_configs", ApplicationGatewayConfig),
        ("routing_policies", RoutingPolicy),
        ("gateway_requests", GatewayRequestRecord),
        ("runtime_operations", RuntimeOperationRecord),
        ("model_executions", ModelExecutionRecord),
        ("usage_events", UsageEventRecord),
        ("billing_transactions", BillingTransactionRecord),
    ):
        actual_counts[name] = await _count_rows(session, model)
    expected_counts = dict(before_counts)
    expected_counts["providers"] -= 1
    expected_counts["models"] -= 2
    expected_counts["provider_credentials"] -= 1
    if actual_counts != expected_counts:
        _raise_precondition("table_row_count_postcondition_failed", tables=len(actual_counts))
    return changed_counts


async def apply_with_backup(
    session: AsyncSession,
    crypto: CredentialCrypto,
    database_url: URL,
    backup_path: Path,
    *,
    expected_fingerprint: str,
) -> dict[str, int]:
    """Validate a fresh dump before opening the caller-owned apply transaction."""
    if session.in_transaction():
        _raise_precondition("apply_session_transaction_already_active")
    create_verified_backup(database_url, backup_path)
    async with session.begin():
        return await apply_merge(
            session,
            crypto,
            expected_fingerprint=expected_fingerprint,
        )


async def _run_dry_run() -> int:
    from src.shared.config import get_settings
    from src.shared.database.session import create_engine_from_settings

    settings = get_settings()
    engine = create_engine_from_settings(settings=settings)
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            crypto = CredentialCrypto.from_settings(settings)
            inventory = await inspect_merge(session, crypto)
            print(render_inventory(inventory))
        return 0
    except MergePreconditionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("merge_precondition:inspection_failed", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()


async def _run_apply(backup_path: Path, expected_fingerprint: str) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.shared.config import get_settings
    from src.shared.database.session import create_engine_from_settings

    settings = get_settings()
    engine = create_engine_from_settings(settings=settings)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            crypto = CredentialCrypto.from_settings(settings)
            changed_counts = await apply_with_backup(
                session,
                crypto,
                make_url(settings.database_url),
                backup_path,
                expected_fingerprint=expected_fingerprint,
            )
        print("OpenAI provider merge applied")
        for name, count in sorted(changed_counts.items()):
            print(f"{name}={count}")
        return 0
    except MergePreconditionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("merge_precondition:apply_failed", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Run the default read-only preflight")
    mode.add_argument("--apply", action="store_true", help="Create a verified dump and apply the merge")
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--expected-fingerprint")
    args = parser.parse_args()
    if args.apply:
        if args.backup_path is None:
            parser.error("--apply requires --backup-path")
        if args.expected_fingerprint is None:
            parser.error("--apply requires --expected-fingerprint")
        if re.fullmatch(r"[0-9a-f]{64}", args.expected_fingerprint) is None:
            parser.error("--expected-fingerprint must be a lowercase SHA-256 hex digest")
        raise SystemExit(asyncio.run(_run_apply(args.backup_path, args.expected_fingerprint)))
    if args.backup_path is not None or args.expected_fingerprint is not None:
        parser.error("--backup-path and --expected-fingerprint require --apply")
    raise SystemExit(asyncio.run(_run_dry_run()))


if __name__ == "__main__":
    main()
