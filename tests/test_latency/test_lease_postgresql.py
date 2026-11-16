import asyncio
import importlib.util
import os
import re
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.backend_ai.repositories.latency import LatencyRepository
from src.shared.models.latency import LatencyModelState, LatencyTrainingRunRecord


@pytest.mark.asyncio
async def test_two_postgresql_sessions_claim_only_one_training_lease():
    raw_url = os.getenv("LATENCY_TEST_POSTGRES_URL")
    if not raw_url:
        pytest.skip("set LATENCY_TEST_POSTGRES_URL to an explicitly disposable PostgreSQL database")
    if not importlib.util.find_spec("psycopg"):
        pytest.skip("the psycopg driver is not installed in this environment")
    from datetime import UTC, datetime

    from sqlalchemy.engine import make_url

    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql" or not re.search(r"(test|disposable|latency)", url.database or "", re.I):
        pytest.skip("LATENCY_TEST_POSTGRES_URL must name a disposable test database")
    if not url.drivername.endswith("+psycopg"):
        url = url.set(drivername="postgresql+psycopg")
    schema = f"latency_test_{uuid4().hex}"
    admin_engine = create_async_engine(url)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    await engine.dispose()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(LatencyModelState.__table__.create)
            await connection.run_sync(LatencyTrainingRunRecord.__table__.create)
            await connection.execute(text("INSERT INTO latency_model_state (scope) VALUES ('generation-v1')"))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(UTC)
        async with factory() as a, factory() as b:
            winners = await asyncio.gather(
                LatencyRepository(a).claim_training("worker-a", now, 120),
                LatencyRepository(b).claim_training("worker-b", now, 120),
            )
        assert sum(lease is not None for lease in winners) == 1
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()
