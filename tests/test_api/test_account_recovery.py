from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.backend.routers import account_recovery
from src.shared.database.session import get_db
from tests.service_app import app


@pytest.fixture
def recovery_dependencies(monkeypatch, client):
    database = SimpleNamespace(scalar=AsyncMock(return_value=1))
    mail = AsyncMock()
    mail.post.return_value = httpx.Response(200)

    async def override_db():
        yield database

    @asynccontextmanager
    async def mail_client(**kwargs):
        yield mail

    monkeypatch.setattr(account_recovery, "get_settings", lambda: SimpleNamespace(
        supabase_url="https://auth.example.com", supabase_publishable_key="test-publishable-key",
        cors_origins="http://localhost:3000,https://taskway.example.com",
    ))
    monkeypatch.setattr(account_recovery.httpx, "AsyncClient", mail_client)
    app.dependency_overrides[get_db] = override_db
    yield database, mail
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_recovery_requires_an_email_matching_the_account_database(client, recovery_dependencies):
    database, mail = recovery_dependencies
    database.scalar.return_value = None
    response = await client.post("/api/v1/auth/recovery", json={
        "email": "missing@example.com", "redirect_to": "http://localhost:3000/auth/confirm",
    })
    assert response.status_code == 404
    assert response.json()["detail"] == "emailNotFound"
    mail.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_normalizes_email_and_sends_only_after_matching(client, recovery_dependencies):
    database, mail = recovery_dependencies
    response = await client.post("/api/v1/auth/recovery", json={
        "email": "  Existing@Example.com  ", "redirect_to": "http://localhost:3000/auth/confirm",
    })
    assert response.status_code == 200
    assert response.json() == {"sent": True}
    assert response.headers["cache-control"] == "no-store"
    statement, params = database.scalar.call_args.args
    assert params == {"email": "existing@example.com"}
    assert ":email" in str(statement)
    mail.post.assert_awaited_once_with(
        "https://auth.example.com/auth/v1/recover",
        headers={"apikey": "test-publishable-key"}, json={"email": "existing@example.com"},
        params={"redirect_to": "http://localhost:3000/auth/confirm"},
    )


@pytest.mark.asyncio
async def test_recovery_does_not_send_mail_if_account_verification_fails(client, recovery_dependencies):
    database, mail = recovery_dependencies
    database.scalar.side_effect = SQLAlchemyError("private database error")
    response = await client.post("/api/v1/auth/recovery", json={
        "email": "existing@example.com", "redirect_to": "http://localhost:3000/auth/confirm",
    })
    assert response.status_code == 503
    assert response.json()["detail"] == "recoveryUnavailable"
    assert "private database" not in response.text
    mail.post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status, expected", [(429, 429), (500, 503)])
async def test_recovery_handles_mail_rate_limits_and_failure(client, recovery_dependencies, status, expected):
    _, mail = recovery_dependencies
    mail.post.return_value = httpx.Response(status)
    response = await client.post("/api/v1/auth/recovery", json={
        "email": "existing@example.com", "redirect_to": "http://localhost:3000/auth/confirm",
    })
    assert response.status_code == expected
    assert response.json()["detail"] == ("rateLimited" if status == 429 else "mailUnavailable")


@pytest.mark.asyncio
async def test_recovery_rejects_malformed_email_without_lookup_or_mail(client, recovery_dependencies):
    database, mail = recovery_dependencies
    response = await client.post("/api/v1/auth/recovery", json={
        "email": "not-an-email", "redirect_to": "http://localhost:3000/auth/confirm",
    })
    assert response.status_code == 422
    database.scalar.assert_not_awaited()
    mail.post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_to", [
    "https://evil.example.com/auth/confirm",
    "http://localhost:3000/dashboard",
    "http://localhost:3000/auth/confirm?next=https://evil.example.com",
])
async def test_recovery_rejects_untrusted_redirects(client, recovery_dependencies, redirect_to):
    database, mail = recovery_dependencies
    response = await client.post("/api/v1/auth/recovery", json={
        "email": "existing@example.com", "redirect_to": redirect_to,
    })
    assert response.status_code == 400
    assert response.json()["detail"] == "invalidRedirect"
    database.scalar.assert_not_awaited()
    mail.post.assert_not_awaited()
