from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.clients.ai_backend_client import AIBackendClient
from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.models.audit import AuditEventRecord
from src.shared.models.billing import BillingTransactionRecord
from src.shared.models.gateway import Provider
from src.shared.models.provider_credentials import ProviderCredential
from src.shared.models.runtime import GatewayRequestRecord
from src.shared.services.credential_crypto import CredentialCrypto
from tests.service_app import app


@pytest_asyncio.fixture
async def probe_client(tmp_path: Path, monkeypatch):
    from src.backend.routers import provider_connections as management_router
    from src.backend_ai.dependencies import ai_service_auth
    from src.backend_ai.routers import ai_internal

    key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        gateway_execution_mode="live",
        gateway_openai_enabled=True,
        credential_encryption_key=key,
        ai_backend_url="http://test",
        ai_service_token="probe-service-token",
    )
    crypto = CredentialCrypto.from_settings(settings)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'provider-probe-api.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    provider_id = "probe-openai"
    credential_id = "probe-credential"
    async with factory() as session:
        session.add_all(
            [
                Provider(
                    id=provider_id,
                    name=provider_id,
                    display_name="Probe OpenAI",
                    kind="openai",
                    purpose="target",
                    status="active",
                    base_url="https://api.openai.com/v1",
                    secret_env_var="",
                ),
                ProviderCredential(
                    id=credential_id,
                    provider_id=provider_id,
                    encrypted_api_key=crypto.encrypt(
                        provider_id=provider_id,
                        credential_id=credential_id,
                        version=1,
                        api_key="probe-secret-key",
                    ),
                    version=1,
                    status="active",
                ),
            ]
        )
        await session.commit()

    state = {"permissions": {"models.manage"}}

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(
            id="probe-admin",
            email="probe-admin@example.com",
            roles=("admin",),
            permissions=frozenset(state["permissions"]),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    monkeypatch.setattr(ai_internal, "get_settings", lambda: settings)
    monkeypatch.setattr(ai_service_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(
        management_router,
        "get_ai_backend_client",
        lambda: AIBackendClient(settings, transport=ASGITransport(app=app)),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, factory, state, settings, provider_id
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_probe_requires_manage_permission(probe_client):
    client, _, state, _, provider_id = probe_client
    state["permissions"] = {"models.read"}
    response = await client.post(
        f"/api/v1/gateway/provider-connections/{provider_id}/probe",
        json={"expected_version": 1, "mode": "chat", "model_name": "gpt-test"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_internal_probe_requires_service_token(probe_client):
    client, _, _, _, provider_id = probe_client
    response = await client.post(
        f"/internal/v1/provider-connections/{provider_id}/probe",
        json={"expected_version": 1, "mode": "chat", "model_name": "gpt-test"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_management_ping_forwards_metadata_and_audits_admin(probe_client, monkeypatch):
    from src.backend_ai.services.provider_connection_probe_service import ProviderConnectionProbeService

    client, factory, _, _, provider_id = probe_client
    calls = []

    async def fake_call(self, kind, base_url, api_key, payload):
        calls.append((kind, base_url, api_key, payload.model_name))

    monkeypatch.setattr(ProviderConnectionProbeService, "_call_upstream", fake_call)
    response = await client.post(
        f"/api/v1/gateway/provider-connections/{provider_id}/probe",
        json={"expected_version": 1, "mode": "chat", "model_name": "gpt-test"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "healthy"
    assert calls == [("openai", "https://api.openai.com/v1", "probe-secret-key", "gpt-test")]
    assert "probe-secret-key" not in response.text
    async with factory() as session:
        credential = await session.scalar(select(ProviderCredential))
        event = await session.scalar(select(AuditEventRecord).where(AuditEventRecord.action == "provider_connection.probed"))
        assert credential.last_test_status == "healthy"
        assert credential.last_test_credential_version == 1
        assert event.actor_user_id == "probe-admin"
        assert "probe-secret-key" not in str(event.metadata_json)
        assert await session.scalar(select(GatewayRequestRecord.request_id)) is None
        assert await session.scalar(select(BillingTransactionRecord.id)) is None


@pytest.mark.asyncio
async def test_probe_does_not_accept_raw_api_key(probe_client):
    client, _, _, _, provider_id = probe_client
    response = await client.post(
        f"/api/v1/gateway/provider-connections/{provider_id}/probe",
        json={"expected_version": 1, "mode": "chat", "model_name": "gpt-test", "api_key": "raw-secret"},
    )
    assert response.status_code == 422
    assert "raw-secret" not in response.text


@pytest.mark.asyncio
async def test_stale_probe_result_is_reported_as_conflict(probe_client, monkeypatch):
    from src.backend_ai.services.provider_connection_probe_service import ProviderConnectionProbeService

    client, factory, _, _, provider_id = probe_client

    async def rotate(self, kind, base_url, api_key, payload):
        async with factory() as other:
            credential = await other.scalar(select(ProviderCredential))
            credential.version = 2
            credential.encrypted_api_key = "replaced-by-test"
            await other.commit()

    monkeypatch.setattr(ProviderConnectionProbeService, "_call_upstream", rotate)
    response = await client.post(
        f"/api/v1/gateway/provider-connections/{provider_id}/probe",
        json={"expected_version": 1, "mode": "chat", "model_name": "gpt-test"},
    )

    assert response.status_code == 409
    async with factory() as session:
        credential = await session.scalar(select(ProviderCredential))
        assert credential.version == 2
        assert credential.last_test_credential_version is None
        assert await session.scalar(select(AuditEventRecord).where(AuditEventRecord.action == "provider_connection.probed")) is None


@pytest.mark.asyncio
async def test_ai_backend_outage_maps_to_service_unavailable(probe_client, monkeypatch):
    from src.backend.clients.ai_backend_client import AIBackendUnavailableError
    from src.backend.routers import provider_connections as management_router

    client, _, _, _, provider_id = probe_client

    class UnavailableAI:
        async def post(self, *args, **kwargs):
            raise AIBackendUnavailableError()

    monkeypatch.setattr(management_router, "get_ai_backend_client", lambda: UnavailableAI())
    response = await client.post(
        f"/api/v1/gateway/provider-connections/{provider_id}/probe",
        json={"expected_version": 1, "mode": "chat", "model_name": "gpt-test"},
    )
    assert response.status_code == 503
