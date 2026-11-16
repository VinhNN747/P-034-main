from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.backend_ai.services.openai_operation_fingerprint import build_openai_fingerprint
from src.backend_ai.services.runtime_operation_service import RuntimeOperationService
from src.shared.models.billing import BillingAccountRecord, BillingTransactionRecord
from src.shared.models.core import Application
from src.shared.models.integration import ApplicationApiKeyRecord
from src.shared.models.runtime import RuntimeOperationRecord
from src.shared.services.billing_service import BillingService


def _postgres_url() -> str | None:
    value = os.getenv("DATABASE_URL", "")
    return value if value.startswith("postgresql+") else None


@pytest.mark.asyncio
async def test_postgres_serializes_same_key_claims_to_one_execution_owner():
    database_url = _postgres_url()
    if database_url is None:
        pytest.skip("PostgreSQL concurrency test requires DATABASE_URL to use PostgreSQL")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app_id = str(uuid4())
    key_id = str(uuid4())
    principal = ApplicationPrincipal(application_id=app_id, key_id=key_id)
    try:
        async with factory() as session:
            session.add(
                Application(
                    id=app_id,
                    slug=f"pg-runtime-{app_id[:8]}",
                    name="Postgres runtime concurrency",
                    owner_user_id=f"owner-{uuid4()}",
                )
            )
            session.add(
                ApplicationApiKeyRecord(
                    id=key_id,
                    application_id=app_id,
                    name="test",
                    key_prefix=f"ak_{uuid4().hex[:9]}",
                    key_hash=uuid4().hex + uuid4().hex,
                )
            )
            await session.commit()

        async def claim_once():
            async with factory() as session:
                return await RuntimeOperationService(session).claim(
                    principal,
                    "postgres-concurrent-key",
                    {"prompt": "hello"},
                    config_version=1,
                    config_snapshot={"config": {"version": 1}},
                    execution_mode="offline",
                )

        results = await asyncio.gather(claim_once(), claim_once())
        assert [item.kind for item in results].count("owner") == 1
        assert len({item.operation.request_id for item in results}) == 1
        async with factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(RuntimeOperationRecord)
                .where(RuntimeOperationRecord.application_id == app_id)
            )
            assert count == 1
    finally:
        async with factory() as session:
            await session.execute(delete(RuntimeOperationRecord).where(RuntimeOperationRecord.application_id == app_id))
            await session.execute(delete(ApplicationApiKeyRecord).where(ApplicationApiKeyRecord.id == key_id))
            await session.execute(delete(Application).where(Application.id == app_id))
            await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_serializes_normalized_openai_claims_to_one_execution_owner():
    database_url = _postgres_url()
    if database_url is None:
        pytest.skip("PostgreSQL normalized operation test requires DATABASE_URL to use PostgreSQL")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app_id = str(uuid4())
    key_id = str(uuid4())
    principal = ApplicationPrincipal(application_id=app_id, key_id=key_id)
    fingerprint = build_openai_fingerprint("openai_responses", {"model": "auto", "input": "hello", "stream": False})
    try:
        async with factory() as session:
            session.add(
                Application(
                    id=app_id,
                    slug=f"pg-openai-runtime-{app_id[:8]}",
                    name="Postgres normalized OpenAI runtime concurrency",
                    owner_user_id=f"owner-{uuid4()}",
                )
            )
            session.add(
                ApplicationApiKeyRecord(
                    id=key_id,
                    application_id=app_id,
                    name="test",
                    key_prefix=f"ak_{uuid4().hex[:9]}",
                    key_hash=uuid4().hex + uuid4().hex,
                )
            )
            await session.commit()

        async def claim_once():
            async with factory() as session:
                return await RuntimeOperationService(session).claim_normalized(
                    principal,
                    "postgres-openai-normalized-concurrent-key",
                    fingerprint,
                    config_version=1,
                    config_snapshot={"config": {"version": 1}},
                    execution_mode="offline",
                )

        results = await asyncio.gather(claim_once(), claim_once())
        assert [item.kind for item in results].count("owner") == 1
        assert len({item.operation.request_id for item in results}) == 1
        assert {item.operation.normalization_version for item in results} == {2}
        async with factory() as session:
            operations = list(
                await session.scalars(
                    select(RuntimeOperationRecord).where(RuntimeOperationRecord.application_id == app_id)
                )
            )
            assert len(operations) == 1
            assert operations[0].payload_hash == fingerprint.payload_hash
            assert operations[0].normalization_version == fingerprint.normalization_version
    finally:
        async with factory() as session:
            await session.execute(delete(RuntimeOperationRecord).where(RuntimeOperationRecord.application_id == app_id))
            await session.execute(delete(ApplicationApiKeyRecord).where(ApplicationApiKeyRecord.id == key_id))
            await session.execute(delete(Application).where(Application.id == app_id))
            await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_locks_shared_owner_billing_account_across_applications():
    database_url = _postgres_url()
    if database_url is None:
        pytest.skip("PostgreSQL billing concurrency test requires DATABASE_URL to use PostgreSQL")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    owner_id = f"owner-{uuid4()}"
    application_ids = [str(uuid4()), str(uuid4())]
    request_ids = [str(uuid4()), str(uuid4())]
    account_id = None
    try:
        async with factory() as session:
            for index, application_id in enumerate(application_ids):
                session.add(
                    Application(
                        id=application_id,
                        slug=f"pg-billing-{application_id[:8]}",
                        name=f"Postgres billing concurrency {index}",
                        owner_user_id=owner_id,
                    )
                )
            await session.commit()
            account = await BillingService(session).account(owner_id)
            account_id = account.id

        async def debit(application_id: str, request_id: str):
            async with factory() as session:
                await BillingService(session).debit_runtime_request(application_id, request_id, "0.01", "completed")
                await session.commit()

        await asyncio.gather(
            debit(application_ids[0], request_ids[0]),
            debit(application_ids[1], request_ids[1]),
        )
        async with factory() as session:
            account = await session.get(BillingAccountRecord, account_id)
            transactions = list(
                await session.scalars(
                    select(BillingTransactionRecord).where(
                        BillingTransactionRecord.source_request_id.in_(request_ids)
                    )
                )
            )
            assert account is not None and str(account.lifetime_usage) == "0.020000"
            assert len(transactions) == 2
    finally:
        async with factory() as session:
            await session.execute(
                delete(BillingTransactionRecord).where(BillingTransactionRecord.source_request_id.in_(request_ids))
            )
            if account_id is not None:
                await session.execute(delete(BillingAccountRecord).where(BillingAccountRecord.id == account_id))
            await session.execute(delete(Application).where(Application.id.in_(application_ids)))
            await session.commit()
        await engine.dispose()
