import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.scripts.merge_openai_live_test as merge_script
from src.scripts.merge_openai_live_test import (
    MergePreconditionError,
    apply_merge,
    apply_with_backup,
    create_verified_backup,
    inspect_merge,
    render_inventory,
    rewrite_provider_fields,
)
from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.models import (
    Application,
    ApplicationApiKeyRecord,
    ApplicationGatewayConfig,
    BillingAccountRecord,
    BillingTransactionRecord,
    GatewayRequestRecord,
    Model,
    ModelExecutionRecord,
    ModelPool,
    ModelPoolMember,
    Provider,
    ProviderCredential,
    RoutingPolicy,
    RuntimeOperationRecord,
    ServiceProviderBinding,
    UsageEventRecord,
)
from src.shared.services.credential_crypto import CredentialCrypto

OLD_PROVIDER_ID = "live-provider-id"
OLD_PROVIDER_NAME = "openai-live-20260928103943"
CANONICAL_PROVIDER_ID = "canonical-provider-id"
CANONICAL_PROVIDER_NAME = "openai"
API_KEY = "provider-key-that-must-never-be-printed"
DATABASE_URL = "postgresql://db-user:db-password@db.example.invalid/shared"
USER_ID = "private-user-id"


@dataclass
class MergeFixture:
    engine: object
    session: object
    crypto: CredentialCrypto
    ids: dict[str, str]


@pytest_asyncio.fixture
async def merge_fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session = async_sessionmaker(engine, expire_on_commit=False)()
    crypto = CredentialCrypto.from_settings(
        Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///:memory:",
            credential_encryption_key=Fernet.generate_key().decode("ascii"),
        )
    )
    ids = {
        "live_model_gpt4": "live-model-gpt-4o-mini",
        "live_model_luna": "live-model-gpt-5.6-luna",
        "live_model_terra": "live-model-gpt-5.6-terra",
        "live_model_embedding": "live-model-text-embedding-3-small",
        "canonical_model_gpt4": "canonical-model-gpt-4o-mini",
        "canonical_model_luna": "canonical-model-gpt-5.6-luna",
        "pool": "pool-id",
        "application": "application-id",
    }
    canonical = Provider(
        id=CANONICAL_PROVIDER_ID,
        name="openai",
        display_name="OpenAI",
        kind="openai",
        purpose="target",
        status="active",
    )
    live = Provider(
        id=OLD_PROVIDER_ID,
        name=OLD_PROVIDER_NAME,
        display_name="OpenAI Live Test",
        kind="openai",
        purpose="target",
        status="active",
    )
    session.add_all([canonical, live])
    await session.flush()
    model_specs = [
        (ids["canonical_model_gpt4"], CANONICAL_PROVIDER_ID, "gpt-4o-mini"),
        (ids["canonical_model_luna"], CANONICAL_PROVIDER_ID, "gpt-5.6-luna"),
        (ids["live_model_gpt4"], OLD_PROVIDER_ID, "gpt-4o-mini"),
        (ids["live_model_luna"], OLD_PROVIDER_ID, "gpt-5.6-luna"),
        (ids["live_model_terra"], OLD_PROVIDER_ID, "gpt-5.6-terra"),
        (ids["live_model_embedding"], OLD_PROVIDER_ID, "text-embedding-3-small"),
    ]
    session.add_all(
        [
            Model(
                id=model_id,
                provider_id=provider_id,
                name=name,
                display_name=name,
                status="active",
            )
            for model_id, provider_id, name in model_specs
        ]
    )
    session.add(ModelPool(id=ids["pool"], owner_user_id=USER_ID, name="Terra pool", purpose="test", status="active"))
    await session.flush()
    session.add(
        ModelPoolMember(
            id="terra-pool-member",
            pool_id=ids["pool"],
            model_id=ids["live_model_terra"],
            role="primary",
        )
    )
    session.add(ServiceProviderBinding(purpose="internal_openai", provider_id=OLD_PROVIDER_ID))
    for provider_id, credential_id, api_key in (
        (CANONICAL_PROVIDER_ID, "canonical-credential", API_KEY),
        (OLD_PROVIDER_ID, "live-credential", API_KEY),
    ):
        session.add(
            ProviderCredential(
                id=credential_id,
                provider_id=provider_id,
                encrypted_api_key=crypto.encrypt(
                    provider_id=provider_id,
                    credential_id=credential_id,
                    version=1,
                    api_key=api_key,
                ),
                version=1,
                status="active",
                key_last4="CRET",
            )
        )
    await session.commit()
    yield MergeFixture(engine=engine, session=session, crypto=crypto, ids=ids)
    await session.close()
    await engine.dispose()


@pytest.fixture
def backup_directory():
    with tempfile.TemporaryDirectory(prefix="openai-provider-merge-backup-", dir="/tmp") as path:
        yield Path(path)


async def seed_history(merge_fixture):
    now = datetime.now(UTC)
    application_id = "history-application"
    application_key_id = "history-api-key"
    billing_account_id = "history-billing-account"
    merge_fixture.session.add(
        Application(
            id=application_id,
            slug="history-app",
            name="History application",
            owner_user_id=USER_ID,
            environment="development",
            status="active",
        )
    )
    await merge_fixture.session.flush()
    merge_fixture.session.add(
        ApplicationApiKeyRecord(
            id=application_key_id,
            application_id=application_id,
            name="history-key",
            key_prefix="history-prefix",
            key_hash="history-hash",
        )
    )
    merge_fixture.session.add(
        BillingAccountRecord(id=billing_account_id, user_id="billing-owner", balance=Decimal("100.000000"))
    )
    await merge_fixture.session.flush()

    request_ids = [f"history-request-{index:02d}" for index in range(22)]
    gateway_requests = []
    for index, request_id in enumerate(request_ids):
        response = {"output": f"preserve-gateway-output-{index}"}
        strategies = {"embedding_provider": OLD_PROVIDER_NAME} if index < 20 else {}
        if index >= 20:
            response["allowed_providers"] = [OLD_PROVIDER_ID, OLD_PROVIDER_NAME, "anthropic"]
        gateway_requests.append(
            GatewayRequestRecord(
                request_id=request_id,
                application_id=application_id,
                prompt=f"preserve-prompt-{index}",
                strategies_json=strategies,
                response_json=response,
                status="completed",
                total_cost=0.125,
                started_at=now,
            )
        )
    merge_fixture.session.add_all(gateway_requests)
    await merge_fixture.session.flush()

    merge_fixture.session.add_all(
        [
            ModelExecutionRecord(
                id=f"execution-{index:02d}",
                request_id=request_ids[index],
                task_id="task-1",
                provider=OLD_PROVIDER_NAME,
                model="gpt-4o-mini",
                status="succeeded",
                result_payload=f"preserve-execution-output-{index}",
            )
            for index in range(20)
        ]
    )
    merge_fixture.session.add_all(
        [
            UsageEventRecord(
                id=f"usage-{index:02d}",
                request_id=request_ids[index],
                application_id=application_id,
                provider=OLD_PROVIDER_NAME,
                model="gpt-4o-mini",
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.125,
            )
            for index in range(20)
        ]
    )
    merge_fixture.session.add_all(
        [
            BillingTransactionRecord(
                id=f"billing-{index:02d}",
                account_id=billing_account_id,
                transaction_type="usage_debit",
                amount=Decimal("-0.125000"),
                currency="USD",
                status="completed",
                source_request_id=request_id,
                description=f"preserve-billing-{index}",
                metadata_json={"preserve": True},
            )
            for index, request_id in enumerate(request_ids)
        ]
    )

    runtime_operations = []
    for index in range(30):
        if index < 28:
            snapshot = {
                "model": {
                    "provider_id": OLD_PROVIDER_ID,
                    "provider_name": OLD_PROVIDER_NAME,
                    "id": "historic-live-model-id",
                },
                "request": {"model_id": "historic-live-model-id"},
            }
        else:
            snapshot = {
                "models": [
                    {
                        "provider_id": OLD_PROVIDER_ID,
                        "provider_name": OLD_PROVIDER_NAME,
                        "id": "historic-live-model-id",
                    }
                ]
            }
        public_result = (
            {"providers_used": [OLD_PROVIDER_NAME], "output": f"preserve-runtime-output-{index}"}
            if index < 20
            else None
        )
        runtime_operations.append(
            RuntimeOperationRecord(
                request_id=f"runtime-request-{index:02d}",
                application_id=application_id,
                application_api_key_id=application_key_id,
                idempotency_key_hash=f"idempotency-{index:02d}",
                payload_hash=f"payload-{index:02d}",
                status="completed" if index < 22 else "indeterminate",
                execution_mode="live",
                config_version=1,
                config_snapshot_json=snapshot,
                public_result_json=public_result,
                started_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
    merge_fixture.session.add_all(runtime_operations)
    await merge_fixture.session.commit()
    return {
        "application_id": application_id,
        "request_ids": request_ids,
        "runtime_ids": [f"runtime-request-{index:02d}" for index in range(30)],
    }


def test_rewrite_provider_fields_only():
    source = {
        "strategies_json": {"embedding_provider": OLD_PROVIDER_NAME},
        "response_json": {
            "constraint_snapshot": {"allowed_providers": [OLD_PROVIDER_ID, OLD_PROVIDER_NAME, "anthropic"]}
        },
        "config_snapshot_json": {
            "model": {
                "provider_id": OLD_PROVIDER_ID,
                "provider_name": OLD_PROVIDER_NAME,
                "id": "historic-live-test-model-id",
            },
            "models": [
                {
                    "provider_id": OLD_PROVIDER_ID,
                    "provider_name": OLD_PROVIDER_NAME,
                    "model_id": "historic-live-test-model-id",
                }
            ],
        },
        "public_result_json": {"providers_used": [OLD_PROVIDER_NAME, "anthropic"]},
        "prompt": f"Please use {OLD_PROVIDER_NAME} for this request.",
        "output": f"Result generated by {OLD_PROVIDER_NAME}.",
        "model_id": "historic-live-test-model-id",
        "unrelated_provider_id": "another-provider-id",
    }

    rewritten, count = rewrite_provider_fields(
        source,
        old_provider_id=OLD_PROVIDER_ID,
        old_provider_name=OLD_PROVIDER_NAME,
        canonical_provider_id=CANONICAL_PROVIDER_ID,
        canonical_provider_name=CANONICAL_PROVIDER_NAME,
    )

    assert count == 8
    assert rewritten == {
        "strategies_json": {"embedding_provider": CANONICAL_PROVIDER_NAME},
        "response_json": {
            "constraint_snapshot": {"allowed_providers": [CANONICAL_PROVIDER_ID, CANONICAL_PROVIDER_NAME, "anthropic"]}
        },
        "config_snapshot_json": {
            "model": {
                "provider_id": CANONICAL_PROVIDER_ID,
                "provider_name": CANONICAL_PROVIDER_NAME,
                "id": "historic-live-test-model-id",
            },
            "models": [
                {
                    "provider_id": CANONICAL_PROVIDER_ID,
                    "provider_name": CANONICAL_PROVIDER_NAME,
                    "model_id": "historic-live-test-model-id",
                }
            ],
        },
        "public_result_json": {"providers_used": [CANONICAL_PROVIDER_NAME, "anthropic"]},
        "prompt": f"Please use {OLD_PROVIDER_NAME} for this request.",
        "output": f"Result generated by {OLD_PROVIDER_NAME}.",
        "model_id": "historic-live-test-model-id",
        "unrelated_provider_id": "another-provider-id",
    }
    assert source["config_snapshot_json"]["model"]["provider_id"] == OLD_PROVIDER_ID


def test_rewrite_is_idempotent():
    source = {
        "embedding_provider": OLD_PROVIDER_NAME,
        "model": {"provider_id": OLD_PROVIDER_ID, "provider_name": OLD_PROVIDER_NAME},
        "providers_used": [OLD_PROVIDER_NAME],
    }
    kwargs = {
        "old_provider_id": OLD_PROVIDER_ID,
        "old_provider_name": OLD_PROVIDER_NAME,
        "canonical_provider_id": CANONICAL_PROVIDER_ID,
        "canonical_provider_name": CANONICAL_PROVIDER_NAME,
    }

    first, first_count = rewrite_provider_fields(source, **kwargs)
    second, second_count = rewrite_provider_fields(first, **kwargs)

    assert first_count == 4
    assert second_count == 0
    assert second == first


def test_unknown_provider_like_text_is_unchanged():
    source = {
        "description": f"alias={OLD_PROVIDER_NAME}; id={OLD_PROVIDER_ID}",
        "nested": {"note": f"Do not rewrite {OLD_PROVIDER_NAME} in prose."},
    }

    rewritten, count = rewrite_provider_fields(
        source,
        old_provider_id=OLD_PROVIDER_ID,
        old_provider_name=OLD_PROVIDER_NAME,
        canonical_provider_id=CANONICAL_PROVIDER_ID,
        canonical_provider_name=CANONICAL_PROVIDER_NAME,
    )

    assert count == 0
    assert rewritten == source


def test_cli_help_runs_from_repo_root():
    project_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(project_root / "src" / "scripts" / "merge_openai_live_test.py"), "--help"],
        cwd=project_root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    assert "--dry-run" in result.stdout


@pytest.mark.asyncio
async def test_preflight_builds_expected_inventory(merge_fixture):
    inventory = await inspect_merge(merge_fixture.session, merge_fixture.crypto)

    assert set(inventory.model_ids_to_move) == {
        merge_fixture.ids["live_model_terra"],
        merge_fixture.ids["live_model_embedding"],
    }
    assert set(inventory.model_ids_to_delete) == {
        merge_fixture.ids["live_model_gpt4"],
        merge_fixture.ids["live_model_luna"],
    }
    assert inventory.binding_purposes == ("internal_openai",)
    assert inventory.routing_policy_fallback_rewrites == ()
    assert len(inventory.fingerprint) == 64


@pytest.mark.asyncio
async def test_preflight_accepts_only_the_approved_routing_policy_fallback(merge_fixture):
    policy = RoutingPolicy(
        id="approved-policy-id",
        name="policy-live-20260928103943",
        status="active",
        fallback_chain_json=[
            "existing-fallback-before",
            merge_fixture.ids["live_model_gpt4"],
            "existing-fallback-after",
        ],
    )
    merge_fixture.session.add(policy)
    await merge_fixture.session.commit()

    inventory = await inspect_merge(merge_fixture.session, merge_fixture.crypto)

    assert len(inventory.routing_policy_fallback_rewrites) == 1
    rewrite = inventory.routing_policy_fallback_rewrites[0]
    assert rewrite.policy_id == "approved-policy-id"
    assert rewrite.policy_name == "policy-live-20260928103943"
    assert rewrite.duplicate_model_id == merge_fixture.ids["live_model_gpt4"]
    assert rewrite.canonical_model_id == merge_fixture.ids["canonical_model_gpt4"]
    assert rewrite.old_fallback_chain == (
        "existing-fallback-before",
        merge_fixture.ids["live_model_gpt4"],
        "existing-fallback-after",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_name", "fallback_chain"),
    [
        ("unapproved-policy", ["live-model-gpt-4o-mini"]),
        ("policy-live-20260928103943", ["live-model-gpt-5.6-luna"]),
        ("policy-live-20260928103943", ["live-model-gpt-4o-mini", "live-model-gpt-4o-mini"]),
    ],
)
async def test_preflight_rejects_unapproved_routing_policy_fallbacks(merge_fixture, policy_name, fallback_chain):
    merge_fixture.session.add(
        RoutingPolicy(
            id="unapproved-policy-id",
            name=policy_name,
            status="active",
            fallback_chain_json=fallback_chain,
        )
    )
    await merge_fixture.session.commit()

    with pytest.raises(MergePreconditionError, match="unexpected_model_reference"):
        await inspect_merge(merge_fixture.session, merge_fixture.crypto)


@pytest.mark.asyncio
async def test_preflight_is_read_only(merge_fixture):
    tables = (Provider, Model, ProviderCredential, ServiceProviderBinding)
    before = {
        model.__tablename__: await merge_fixture.session.scalar(select(func.count()).select_from(model))
        for model in tables
    }

    await inspect_merge(merge_fixture.session, merge_fixture.crypto)

    after = {
        model.__tablename__: await merge_fixture.session.scalar(select(func.count()).select_from(model))
        for model in tables
    }
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["canonical_missing", "live_missing", "live_ambiguous"])
async def test_preflight_rejects_missing_or_ambiguous_provider(merge_fixture, case):
    provider_id = CANONICAL_PROVIDER_ID if case == "canonical_missing" else OLD_PROVIDER_ID
    provider = await merge_fixture.session.get(Provider, provider_id)
    if case == "canonical_missing":
        provider.name = "renamed-openai"
        provider.display_name = "Renamed OpenAI"
    elif case == "live_missing":
        provider.name = "renamed-live-provider"
        provider.display_name = "Renamed Live Provider"
    else:
        merge_fixture.session.add(
            Provider(
                id="ambiguous-live-provider",
                name="openai-live-extra",
                display_name="OpenAI Live Test",
                kind="openai",
                purpose="target",
                status="active",
            )
        )
    await merge_fixture.session.commit()

    with pytest.raises(MergePreconditionError, match="provider"):
        await inspect_merge(merge_fixture.session, merge_fixture.crypto)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["canonical_inactive", "live_wrong_kind"])
async def test_preflight_rejects_invalid_provider_status_or_kind(merge_fixture, case):
    if case == "canonical_inactive":
        provider = await merge_fixture.session.get(Provider, CANONICAL_PROVIDER_ID)
        provider.status = "inactive"
    else:
        provider = await merge_fixture.session.get(Provider, OLD_PROVIDER_ID)
        provider.kind = "anthropic"
    await merge_fixture.session.commit()

    with pytest.raises(MergePreconditionError, match="provider_identity_or_status_invalid"):
        await inspect_merge(merge_fixture.session, merge_fixture.crypto)


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_problem", ["missing_crypto", "revoked", "missing", "undecryptable", "different"])
async def test_preflight_rejects_unusable_or_different_credentials(merge_fixture, credential_problem):
    if credential_problem == "missing_crypto":
        crypto = None
    else:
        crypto = merge_fixture.crypto
        credential = await merge_fixture.session.scalar(
            select(ProviderCredential).where(ProviderCredential.provider_id == OLD_PROVIDER_ID)
        )
        if credential_problem == "revoked":
            credential.status = "revoked"
        elif credential_problem == "missing":
            await merge_fixture.session.delete(credential)
        elif credential_problem == "undecryptable":
            credential.encrypted_api_key = "not-valid-ciphertext"
        elif credential_problem == "different":
            credential.encrypted_api_key = merge_fixture.crypto.encrypt(
                provider_id=OLD_PROVIDER_ID,
                credential_id=credential.id,
                version=credential.version,
                api_key="a-different-provider-key",
            )
        await merge_fixture.session.commit()

    with pytest.raises(MergePreconditionError, match="credential") as error:
        await inspect_merge(merge_fixture.session, crypto)
    assert API_KEY not in str(error.value)
    assert "a-different-provider-key" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["pool", "fallback"])
async def test_preflight_rejects_unexpected_live_model_references(merge_fixture, reference):
    duplicate_model_id = merge_fixture.ids["live_model_gpt4"]
    if reference == "pool":
        merge_fixture.session.add(
            ModelPoolMember(
                id="unexpected-duplicate-member",
                pool_id=merge_fixture.ids["pool"],
                model_id=duplicate_model_id,
                role="fallback",
            )
        )
    else:
        application = Application(
            id=merge_fixture.ids["application"],
            slug="test-app",
            name="Test app",
            owner_user_id=USER_ID,
            environment="development",
            status="active",
        )
        merge_fixture.session.add(application)
        await merge_fixture.session.flush()
        merge_fixture.session.add(
            ApplicationGatewayConfig(
                id="app-config",
                application_id=application.id,
                fallback_model_id=duplicate_model_id,
            )
        )
    await merge_fixture.session.commit()

    with pytest.raises(MergePreconditionError, match="reference"):
        await inspect_merge(merge_fixture.session, merge_fixture.crypto)


@pytest.mark.asyncio
async def test_dry_run_summary_redacts_secrets_and_personal_ids(merge_fixture):
    inventory = await inspect_merge(merge_fixture.session, merge_fixture.crypto)
    live_credential = await merge_fixture.session.scalar(
        select(ProviderCredential).where(ProviderCredential.provider_id == OLD_PROVIDER_ID)
    )

    summary = render_inventory(inventory)

    assert "OpenAI Live Test" in summary
    assert "gpt-5.6-terra" in summary
    assert API_KEY not in summary
    assert DATABASE_URL not in summary
    assert "canonical-provider-id" not in summary
    assert "live-provider-id" not in summary
    assert "canonical-credential" not in summary
    assert live_credential.encrypted_api_key not in summary
    assert USER_ID not in summary


def test_create_verified_backup_uses_custom_dump_and_keeps_password_out_of_argv(backup_directory, monkeypatch):
    calls = []

    def fake_run(args, *, env, capture_output, text, check):
        calls.append((args, env, capture_output, text, check))
        if args[0] == "pg_dump":
            Path(args[args.index("--file") + 1]).write_bytes(b"PGDMP-test-dump")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(merge_script.subprocess, "run", fake_run)
    destination = backup_directory / "verified.pgdump"

    create_verified_backup(make_url(DATABASE_URL), destination)

    dump_args, dump_env, capture_output, text_mode, check = calls[0]
    assert dump_args[:3] == ["pg_dump", "--format=custom", "--no-password"]
    assert "--list" in calls[1][0]
    assert DATABASE_URL not in " ".join(dump_args)
    assert "db-password" not in " ".join(dump_args)
    assert dump_env["PGPASSWORD"] == "db-password"
    assert capture_output and text_mode and not check
    assert destination.read_bytes() == b"PGDMP-test-dump"
    assert destination.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_apply_requires_verified_backup_and_matching_inventory(merge_fixture, backup_directory, monkeypatch):
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "src" / "scripts" / "merge_openai_live_test.py"),
            "--apply",
            "--expected-fingerprint",
            "0" * 64,
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "backup-path" in result.stderr

    before = await merge_fixture.session.scalar(select(func.count()).select_from(Model))
    await merge_fixture.session.commit()

    def invalid_dump(args, **kwargs):
        if args[0] == "pg_dump":
            Path(args[args.index("--file") + 1]).write_bytes(b"partial")
            return subprocess.CompletedProcess(args, 1, "", "database-url-and-provider-secret")
        return subprocess.CompletedProcess(args, 1, "", "invalid backup")

    monkeypatch.setattr(merge_script.subprocess, "run", invalid_dump)
    with pytest.raises(MergePreconditionError, match="backup"):
        await apply_with_backup(
            merge_fixture.session,
            merge_fixture.crypto,
            make_url(DATABASE_URL),
            backup_directory / "failed-dump.pgdump",
            expected_fingerprint="0" * 64,
        )
    after = await merge_fixture.session.scalar(select(func.count()).select_from(Model))
    assert after == before

    inventory = await inspect_merge(merge_fixture.session, merge_fixture.crypto)
    await merge_fixture.session.commit()
    monkeypatch.setattr(merge_script, "create_verified_backup", lambda *_args: None)
    with pytest.raises(MergePreconditionError, match="fingerprint"):
        await apply_with_backup(
            merge_fixture.session,
            merge_fixture.crypto,
            make_url(DATABASE_URL),
            backup_directory / "fake-verified-backup.pgdump",
            expected_fingerprint="0" * 64 if inventory.fingerprint != "0" * 64 else "1" * 64,
        )
    after_mismatch = await merge_fixture.session.scalar(select(func.count()).select_from(Model))
    assert after_mismatch == before


@pytest.mark.asyncio
async def test_apply_moves_models_rebinds_internal_service_and_deletes_duplicate(merge_fixture):
    inventory = await inspect_merge(merge_fixture.session, merge_fixture.crypto)
    canonical_models_before = {
        row.id: (row.provider_id, row.name, row.display_name, row.capabilities_json, row.pricing_version)
        for row in (
            await merge_fixture.session.scalars(
                select(Model).where(Model.provider_id == CANONICAL_PROVIDER_ID).order_by(Model.id)
            )
        ).all()
    }
    canonical_credential_before = await merge_fixture.session.get(ProviderCredential, "canonical-credential")
    canonical_ciphertext = canonical_credential_before.encrypted_api_key
    terra_members_before = (
        await merge_fixture.session.execute(
            select(ModelPoolMember.id, ModelPoolMember.pool_id, ModelPoolMember.model_id, ModelPoolMember.role).where(
                ModelPoolMember.model_id == merge_fixture.ids["live_model_terra"]
            )
        )
    ).all()
    await merge_fixture.session.commit()

    async with merge_fixture.session.begin():
        changed = await apply_merge(
            merge_fixture.session,
            merge_fixture.crypto,
            expected_fingerprint=inventory.fingerprint,
        )

    assert changed["models_moved"] == 2
    assert changed["models_deleted"] == 2
    assert changed["provider_credentials_deleted"] == 1
    assert changed["providers_deleted"] == 1
    assert (await merge_fixture.session.get(Provider, OLD_PROVIDER_ID)) is None
    assert (await merge_fixture.session.get(ProviderCredential, "live-credential")) is None
    assert (
        await merge_fixture.session.get(ProviderCredential, "canonical-credential")
    ).encrypted_api_key == canonical_ciphertext
    binding = await merge_fixture.session.get(ServiceProviderBinding, "internal_openai")
    assert binding.provider_id == CANONICAL_PROVIDER_ID
    for model_id in inventory.model_ids_to_move:
        assert (await merge_fixture.session.get(Model, model_id)).provider_id == CANONICAL_PROVIDER_ID
    for model_id in inventory.model_ids_to_delete:
        assert await merge_fixture.session.get(Model, model_id) is None
    canonical_models_after = {
        row.id: (row.provider_id, row.name, row.display_name, row.capabilities_json, row.pricing_version)
        for row in (
            await merge_fixture.session.scalars(
                select(Model).where(Model.provider_id == CANONICAL_PROVIDER_ID).order_by(Model.id)
            )
        ).all()
        if row.id in canonical_models_before
    }
    assert canonical_models_after == canonical_models_before
    terra_members_after = (
        await merge_fixture.session.execute(
            select(ModelPoolMember.id, ModelPoolMember.pool_id, ModelPoolMember.model_id, ModelPoolMember.role).where(
                ModelPoolMember.model_id == merge_fixture.ids["live_model_terra"]
            )
        )
    ).all()
    assert terra_members_after == terra_members_before


@pytest.mark.asyncio
async def test_apply_remaps_approved_routing_policy_fallback_and_preserves_order(merge_fixture):
    old_chain = ["existing-fallback-before", merge_fixture.ids["live_model_gpt4"], "existing-fallback-after"]
    merge_fixture.session.add(
        RoutingPolicy(
            id="approved-policy-id",
            name="policy-live-20260928103943",
            status="active",
            fallback_chain_json=old_chain,
        )
    )
    await merge_fixture.session.commit()
    inventory = await inspect_merge(merge_fixture.session, merge_fixture.crypto)
    await merge_fixture.session.commit()

    async with merge_fixture.session.begin():
        changed = await apply_merge(
            merge_fixture.session,
            merge_fixture.crypto,
            expected_fingerprint=inventory.fingerprint,
        )

    policy = await merge_fixture.session.get(RoutingPolicy, "approved-policy-id")
    assert changed["routing_policy_fallbacks_remapped"] == 1
    assert policy.name == "policy-live-20260928103943"
    assert policy.status == "active"
    assert policy.fallback_chain_json == [
        "existing-fallback-before",
        merge_fixture.ids["canonical_model_gpt4"],
        "existing-fallback-after",
    ]


@pytest.mark.asyncio
async def test_apply_rewrites_only_provider_history_fields(merge_fixture):
    history = await seed_history(merge_fixture)
    inventory = await inspect_merge(merge_fixture.session, merge_fixture.crypto)
    affected = dict(inventory.affected_row_counts)
    assert affected == {
        "gateway_requests.strategies_json": 20,
        "gateway_requests.response_json": 2,
        "runtime_operations.config_snapshot_json": 30,
        "runtime_operations.public_result_json": 20,
        "model_executions.provider": 20,
        "usage_events.provider": 20,
    }
    request_before = {
        row.request_id: (row.prompt, row.response_json.get("output"))
        for row in (await merge_fixture.session.scalars(select(GatewayRequestRecord))).all()
    }
    billing_before = [
        (row.id, row.source_request_id, row.amount, row.status, row.metadata_json)
        for row in (
            await merge_fixture.session.scalars(select(BillingTransactionRecord).order_by(BillingTransactionRecord.id))
        ).all()
    ]
    await merge_fixture.session.commit()

    async with merge_fixture.session.begin():
        changed = await apply_merge(
            merge_fixture.session,
            merge_fixture.crypto,
            expected_fingerprint=inventory.fingerprint,
        )

    assert changed["gateway_requests.strategies_json"] == 20
    assert changed["gateway_requests.response_json"] == 2
    assert changed["runtime_operations.config_snapshot_json"] == 30
    assert changed["runtime_operations.public_result_json"] == 20
    assert changed["model_executions.provider"] == 20
    assert changed["usage_events.provider"] == 20
    assert set(history["request_ids"]) == {
        row.request_id for row in (await merge_fixture.session.scalars(select(GatewayRequestRecord))).all()
    }
    requests_after = (await merge_fixture.session.scalars(select(GatewayRequestRecord))).all()
    assert {row.request_id: (row.prompt, row.response_json.get("output")) for row in requests_after} == request_before
    assert all(row.strategies_json.get("embedding_provider") == CANONICAL_PROVIDER_NAME for row in requests_after[:20])
    assert requests_after[20].response_json["allowed_providers"] == [
        CANONICAL_PROVIDER_ID,
        CANONICAL_PROVIDER_NAME,
        "anthropic",
    ]
    executions = (await merge_fixture.session.scalars(select(ModelExecutionRecord))).all()
    usage = (await merge_fixture.session.scalars(select(UsageEventRecord))).all()
    assert all(row.provider == CANONICAL_PROVIDER_NAME for row in executions + usage)
    runtime_rows = (
        await merge_fixture.session.scalars(select(RuntimeOperationRecord).order_by(RuntimeOperationRecord.request_id))
    ).all()
    assert len(runtime_rows) == 30
    assert runtime_rows[0].config_snapshot_json["model"]["provider_id"] == CANONICAL_PROVIDER_ID
    assert runtime_rows[0].config_snapshot_json["model"]["provider_name"] == CANONICAL_PROVIDER_NAME
    assert runtime_rows[0].config_snapshot_json["model"]["id"] == "historic-live-model-id"
    assert runtime_rows[0].config_snapshot_json["request"]["model_id"] == "historic-live-model-id"
    assert runtime_rows[0].public_result_json["providers_used"] == [CANONICAL_PROVIDER_NAME]
    assert runtime_rows[-1].config_snapshot_json["models"][0]["provider_id"] == CANONICAL_PROVIDER_ID
    billing_after = [
        (row.id, row.source_request_id, row.amount, row.status, row.metadata_json)
        for row in (
            await merge_fixture.session.scalars(select(BillingTransactionRecord).order_by(BillingTransactionRecord.id))
        ).all()
    ]
    assert billing_after == billing_before
    assert len(billing_after) == 22


@pytest.mark.asyncio
async def test_apply_rolls_back_on_unknown_reference(merge_fixture):
    inventory = await inspect_merge(merge_fixture.session, merge_fixture.crypto)
    await merge_fixture.session.commit()
    merge_fixture.session.add(
        ModelPoolMember(
            id="new-duplicate-model-reference",
            pool_id=merge_fixture.ids["pool"],
            model_id=merge_fixture.ids["live_model_gpt4"],
            role="fallback",
        )
    )
    await merge_fixture.session.commit()
    before_models = await merge_fixture.session.scalar(select(func.count()).select_from(Model))
    await merge_fixture.session.commit()

    with pytest.raises(MergePreconditionError, match="unexpected_model_reference"):
        async with merge_fixture.session.begin():
            await apply_merge(
                merge_fixture.session,
                merge_fixture.crypto,
                expected_fingerprint=inventory.fingerprint,
            )

    assert await merge_fixture.session.get(Provider, OLD_PROVIDER_ID) is not None
    assert await merge_fixture.session.get(ProviderCredential, "live-credential") is not None
    assert (await merge_fixture.session.get(ServiceProviderBinding, "internal_openai")).provider_id == OLD_PROVIDER_ID
    assert await merge_fixture.session.scalar(select(func.count()).select_from(Model)) == before_models


@pytest.mark.asyncio
async def test_apply_rolls_back_after_database_error(merge_fixture):
    inventory = await inspect_merge(merge_fixture.session, merge_fixture.crypto)
    await merge_fixture.session.commit()
    await merge_fixture.session.execute(
        text(
            "CREATE TRIGGER prevent_live_provider_delete BEFORE DELETE ON providers "
            "WHEN OLD.id = 'live-provider-id' BEGIN SELECT RAISE(ABORT, 'forced rollback'); END"
        )
    )
    await merge_fixture.session.commit()
    model_count_before = await merge_fixture.session.scalar(select(func.count()).select_from(Model))
    await merge_fixture.session.commit()

    with pytest.raises(Exception):
        async with merge_fixture.session.begin():
            await apply_merge(
                merge_fixture.session,
                merge_fixture.crypto,
                expected_fingerprint=inventory.fingerprint,
            )

    assert await merge_fixture.session.get(Provider, OLD_PROVIDER_ID) is not None
    assert await merge_fixture.session.get(ProviderCredential, "live-credential") is not None
    assert (await merge_fixture.session.get(ServiceProviderBinding, "internal_openai")).provider_id == OLD_PROVIDER_ID
    assert await merge_fixture.session.scalar(select(func.count()).select_from(Model)) == model_count_before


@pytest.mark.asyncio
async def test_backup_failure_aborts_without_database_writes(merge_fixture, backup_directory, monkeypatch):
    before = await merge_fixture.session.scalar(select(func.count()).select_from(Model))
    await merge_fixture.session.commit()
    apply_called = False

    async def unexpected_apply(*_args, **_kwargs):
        nonlocal apply_called
        apply_called = True

    def failed_dump(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "untrusted provider-secret and database-url")

    monkeypatch.setattr(merge_script.subprocess, "run", failed_dump)
    monkeypatch.setattr(merge_script, "apply_merge", unexpected_apply)
    with pytest.raises(MergePreconditionError, match="backup"):
        await apply_with_backup(
            merge_fixture.session,
            merge_fixture.crypto,
            make_url(DATABASE_URL),
            backup_directory / "failed-before-apply.pgdump",
            expected_fingerprint="0" * 64,
        )
    after = await merge_fixture.session.scalar(select(func.count()).select_from(Model))
    assert after == before
    assert apply_called is False


def test_errors_do_not_include_database_url_or_credentials(backup_directory, monkeypatch):
    def failed_dump(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", f"{DATABASE_URL} {API_KEY}")

    monkeypatch.setattr(merge_script.subprocess, "run", failed_dump)
    with pytest.raises(MergePreconditionError) as error:
        create_verified_backup(make_url(DATABASE_URL), backup_directory / "safe-error.pgdump")

    assert DATABASE_URL not in str(error.value)
    assert "db-password" not in str(error.value)
    assert API_KEY not in str(error.value)
