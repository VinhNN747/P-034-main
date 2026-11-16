from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.database.base import Base
from src.shared.models.billing import BillingTransactionRecord
from src.shared.models.core import Application
from src.shared.services.billing_service import BillingService


@pytest_asyncio.fixture
async def billing_session(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "ledger.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            Application(
                id="app-1",
                slug="billing-app",
                name="Billing App",
                owner_user_id="user-1",
                environment="production",
                status="active",
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_completed_runtime_debit_is_idempotent_and_partial_or_failed_are_free(
    billing_session: AsyncSession,
):
    service = BillingService(billing_session)
    first = await service.debit_runtime_request("app-1", "req-1", "2.50", "completed")
    repeated = await service.debit_runtime_request("app-1", "req-1", "2.50", "completed")
    partial = await service.debit_runtime_request("app-1", "req-2", "3.00", "partial")
    failed = await service.debit_runtime_request("app-1", "req-3", "4.00", "failed")
    await billing_session.commit()

    assert first is not None
    assert repeated.id == first.id
    assert partial is None
    assert failed is None
    transactions = list(
        await billing_session.scalars(
            select(BillingTransactionRecord).where(BillingTransactionRecord.transaction_type == "usage_debit")
        )
    )
    assert len(transactions) == 1
    assert float(transactions[0].amount) == -2.5
    account = await service.account("user-1")
    assert float(account.balance) == -2.5
    assert float(account.lifetime_usage) == 2.5
