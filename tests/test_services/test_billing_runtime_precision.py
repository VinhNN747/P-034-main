from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.database.base import Base
from src.shared.models.core import Application
from src.shared.services.billing_service import BillingService


@pytest_asyncio.fixture
async def precision_billing_session(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "precision-ledger.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            Application(
                id="precision-app",
                slug="precision-billing-app",
                name="Precision Billing App",
                owner_user_id="precision-user",
                environment="development",
                status="active",
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_debit_rounds_sub_micro_cost_up_to_ledger_precision(precision_billing_session: AsyncSession):
    transaction = await BillingService(precision_billing_session).debit_runtime_request(
        "precision-app", "precision-request", 0.00000015, "completed"
    )
    await precision_billing_session.commit()

    assert transaction is not None
    assert float(transaction.amount) == pytest.approx(-0.000001)
