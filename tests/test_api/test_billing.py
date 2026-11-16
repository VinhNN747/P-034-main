from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from tests.service_app import app


@pytest_asyncio.fixture
async def billing_client(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "billing.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    permissions: set[str] = {"billing.read", "billing.manage"}

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(
            id="user-1",
            email="user@example.com",
            roles=("owner",),
            permissions=frozenset(permissions),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, permissions
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_billing_account_topup_idempotency_transactions_and_settings(billing_client):
    client, _ = billing_client
    initial = await client.get("/api/v1/billing/account")
    assert initial.status_code == 200
    assert initial.json() == {
        "balance": 0.0,
        "currency": "USD",
        "lifetime_topup": 0.0,
        "lifetime_usage": 0.0,
        "last_transaction": None,
    }

    topup = await client.post(
        "/api/v1/billing/topups",
        json={"amount": "100.10", "currency": "USD", "idempotency_key": "topup-1"},
    )
    assert topup.status_code == 201, topup.text
    first = topup.json()
    assert first["type"] == "simulated_topup"
    assert first["amount"] == 100.10
    assert first["status"] == "completed"
    assert first["simulation"] is True

    repeated = await client.post(
        "/api/v1/billing/topups",
        json={"amount": "100.10", "currency": "USD", "idempotency_key": "topup-1"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["id"] == first["id"]
    assert (await client.get("/api/v1/billing/account")).json()["balance"] == 100.10

    conflict = await client.post(
        "/api/v1/billing/topups",
        json={"amount": "100.11", "currency": "USD", "idempotency_key": "topup-1"},
    )
    assert conflict.status_code == 409

    transactions = await client.get("/api/v1/billing/transactions", params={"page": 1, "page_size": 10})
    assert transactions.status_code == 200
    assert transactions.json()["total"] == 1
    assert transactions.json()["items"][0]["reference"] == "topup-1"
    assert transactions.json()["items"][0]["simulation"] is True

    settings = await client.get("/api/v1/billing/settings")
    assert settings.status_code == 200
    assert settings.json()["notify_low_balance"] is True
    updated = await client.put(
        "/api/v1/billing/settings",
        json={"low_balance_threshold": "25.50", "notify_low_balance": False, "auto_topup_enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["low_balance_threshold"] == 25.5
    assert updated.json()["notify_low_balance"] is False


@pytest.mark.asyncio
async def test_billing_validates_currency_amount_and_permissions(billing_client):
    client, permissions = billing_client
    invalid_amount = await client.post(
        "/api/v1/billing/topups",
        json={"amount": "0", "currency": "USD", "idempotency_key": "bad-amount"},
    )
    invalid_currency = await client.post(
        "/api/v1/billing/topups",
        json={"amount": "1", "currency": "EUR", "idempotency_key": "bad-currency"},
    )
    assert invalid_amount.status_code == 422
    assert invalid_currency.status_code == 422

    permissions.clear()
    assert (await client.get("/api/v1/billing/account")).status_code == 403
    assert (
        await client.post(
            "/api/v1/billing/topups",
            json={"amount": "1", "currency": "USD", "idempotency_key": "denied"},
        )
    ).status_code == 403
