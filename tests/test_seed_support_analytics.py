from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.scripts.seed_support_analytics import seed_session
from src.shared.database.base import Base
from src.shared.models.core import Application
from src.shared.models.runtime import GatewayRequestRecord, UsageEventRecord


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'analytics-seed.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_analytics_seed_is_scoped_varied_and_idempotent(db):
    owned = Application(id="seed-app-a", slug="seed-a", name="Seed A", owner_user_id="owner-a")
    other = Application(id="seed-app-b", slug="seed-b", name="Seed B", owner_user_id="owner-b")
    db.add_all([owned, other])
    await db.commit()

    first = await seed_session(
        db,
        owner_id="owner-a",
        days=20,
        requests_per_day=2,
        end_date=date(2026, 10, 10),
    )
    second = await seed_session(
        db,
        owner_id="owner-a",
        days=20,
        requests_per_day=2,
        end_date=date(2026, 10, 10),
    )

    assert first.applications == 1
    assert first.requested_rows == first.requests_inserted == first.usage_events_inserted == 40
    assert second.requests_inserted == second.usage_events_inserted == 0
    assert await db.scalar(select(func.count()).select_from(GatewayRequestRecord)) == 40
    assert await db.scalar(select(func.count()).select_from(UsageEventRecord)) == 40

    request_rows = list(await db.scalars(select(GatewayRequestRecord)))
    usage_rows = list(await db.scalars(select(UsageEventRecord)))
    assert {row.application_id for row in request_rows} == {owned.id}
    assert {row.status for row in request_rows} == {"completed", "failed"}
    assert {row.model for row in usage_rows} == {
        "gpt-4o-mini",
        "gemini-3.6-flash",
        "gpt-5.6-terra",
    }
    assert {row.source for row in usage_rows} == {"playground", "api", "sdk"}
    assert all(0 < row.cost_usd < 0.1 for row in usage_rows)


@pytest.mark.asyncio
async def test_analytics_seed_rejects_an_unknown_application(db):
    with pytest.raises(ValueError, match="Application not found"):
        await seed_session(db, application_ids=["missing"], days=1, requests_per_day=1)
