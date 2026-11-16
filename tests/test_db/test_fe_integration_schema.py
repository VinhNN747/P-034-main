from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from src.shared.config import get_settings
from src.shared.database.base import Base
from src.shared.models.audit import AuditEventRecord
from src.shared.models.billing import (
    BillingAccountRecord,
    BillingTransactionRecord,
)
from src.shared.repositories.audit import AuditRepository
from src.shared.repositories.billing import BillingRepository


def alembic_config(database_path: Path) -> Config:
    config = Config("src/alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "sqlite+aiosqlite:///" + str(database_path),
    )
    return config


def migrate(
    database_path: Path,
    revision: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "sqlite+aiosqlite:///" + str(database_path),
    )
    get_settings.cache_clear()
    command.upgrade(alembic_config(database_path), revision)
    get_settings.cache_clear()


def table_names(database_path: Path) -> set[str]:
    engine = create_engine("sqlite:///" + str(database_path))
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_fe_integration_migration_adds_tables_and_columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "fresh.db"

    migrate(database_path, "head", monkeypatch)

    tables = table_names(database_path)
    assert {
        "application_api_keys",
        "policy_reviews",
        "policy_decisions",
        "billing_accounts",
        "billing_settings",
        "billing_transactions",
        "audit_events",
    } <= tables

    engine = create_engine("sqlite:///" + str(database_path))
    try:
        config_columns = {column["name"] for column in inspect(engine).get_columns("application_gateway_configs")}
        request_columns = {column["name"] for column in inspect(engine).get_columns("gateway_requests")}
        model_columns = {column["name"] for column in inspect(engine).get_columns("models")}
        assert {
            "routing_objective",
            "fallback_model_ids_json",
            "warning_threshold",
            "budget_action",
            "max_output_tokens",
            "max_latency_ms",
            "timeout_seconds",
            "retry_limit",
            "fallback_enabled",
            "version",
        } <= config_columns
        assert {
            "source",
            "fallback_used",
            "fallback_reason",
            "billing_transaction_id",
        } <= request_columns
        assert {
            "quality_tier",
            "health_status",
            "last_probe_at",
            "last_probe_latency_ms",
            "last_probe_error",
        } <= model_columns
    finally:
        engine.dispose()


def test_fe_integration_migration_preserves_existing_0004_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "existing.db"
    migrate(database_path, "0004_gateway_runtime", monkeypatch)

    engine = create_engine("sqlite:///" + str(database_path))
    application_id = str(uuid4())
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO applications
                    (id, slug, name, description, owner_user_id, environment,
                     status, tags_json, created_at, updated_at)
                    VALUES
                    (:id, :slug, :name, NULL, :owner, 'development',
                     'active', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "id": application_id,
                    "slug": "legacy-app",
                    "name": "Legacy App",
                    "owner": "user-1",
                },
            )
    finally:
        engine.dispose()

    migrate(database_path, "head", monkeypatch)

    engine = create_engine("sqlite:///" + str(database_path))
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT slug, name FROM applications WHERE id = :id"),
                {"id": application_id},
            ).one()
        assert tuple(row) == ("legacy-app", "Legacy App")
    finally:
        engine.dispose()


def test_fe_integration_migration_downgrade_removes_only_new_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "downgrade.db"
    migrate(database_path, "head", monkeypatch)
    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path}",
    )
    get_settings.cache_clear()
    command.downgrade(alembic_config(database_path), "0004_gateway_runtime")
    get_settings.cache_clear()

    tables = table_names(database_path)
    assert "applications" in tables
    assert "gateway_requests" in tables
    assert "billing_accounts" not in tables
    assert "audit_events" not in tables


@pytest_asyncio.fixture
async def integration_session(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "repo.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_billing_and_audit_repositories_commit_local_records(
    integration_session: AsyncSession,
) -> None:
    billing = BillingRepository(integration_session)
    audit = AuditRepository(integration_session)

    account = await billing.get_or_create_account("user-1")
    transaction = BillingTransactionRecord(
        id=str(uuid4()),
        account_id=account.id,
        transaction_type="simulated_topup",
        amount=100,
        currency="USD",
        status="completed",
        idempotency_key="topup-1",
        description="Phase-one simulated top-up",
        metadata_json={"simulation": True},
    )
    await billing.add_transaction(transaction)
    event = await audit.add_event(
        actor_user_id="user-1",
        action="billing.topup",
        resource_type="billing_transaction",
        resource_id=transaction.id,
        request_id="req-1",
        metadata={"simulation": True},
    )
    await integration_session.commit()

    assert await integration_session.get(BillingAccountRecord, account.id)
    assert await integration_session.get(BillingTransactionRecord, transaction.id)
    assert await integration_session.get(AuditEventRecord, event.id)
    assert await billing.by_idempotency_key("topup-1") == transaction
