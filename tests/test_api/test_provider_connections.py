from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.dtos.provider_credentials import ProviderConnectionCreate
from src.backend.services.provider_connection_service import (
    ProviderConnectionInvalidError,
    ProviderConnectionService,
)
from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.models.audit import AuditEventRecord
from src.shared.models.provider_credentials import ProviderCredential
from src.shared.repositories.audit import AuditRepository
from src.shared.services.credential_crypto import CredentialCrypto
from tests.service_app import app


@pytest_asyncio.fixture
async def credential_client(tmp_path: Path):
    from src.backend.routers.provider_connections import get_credential_crypto

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'provider-connections.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    key = Fernet.generate_key().decode()
    crypto = CredentialCrypto.from_settings(Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        credential_encryption_key=key,
    ))
    state = {"permissions": {"models.manage", "models.read", "models.read_available"}}

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(
            id="admin-1",
            email="admin@example.com",
            roles=("admin",),
            permissions=frozenset(state["permissions"]),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_credential_crypto] = lambda: crypto
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, factory, state, crypto
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_user_cannot_manage_connections(credential_client):
    client, _, state, _ = credential_client
    state["permissions"] = {"models.read_available"}

    response = await client.get("/api/v1/gateway/provider-connections")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_legacy_provider_create_requires_manage(credential_client):
    client, _, state, _ = credential_client
    state["permissions"] = {"models.read"}

    response = await client.post("/api/v1/gateway/providers", json={
        "name": "not-admin", "display_name": "Not admin", "kind": "openai",
    })

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_create_encrypts_and_response_masks(credential_client):
    client, factory, _, crypto = credential_client
    secret = "provider-key-sensitive"
    response = await client.post("/api/v1/gateway/provider-connections", json={
        "name": "openai-primary",
        "display_name": "OpenAI Primary",
        "kind": "openai",
        "purpose": "target",
        "api_key": secret,
    })

    assert response.status_code == 201, response.text
    assert secret not in response.text
    assert "encrypted_api_key" not in response.text
    assert response.json()["credential"]["configured"] is True
    async with factory() as session:
        credential = await session.scalar(select(ProviderCredential))
        audit = await session.scalar(select(AuditEventRecord))
        assert credential.encrypted_api_key != secret
        assert crypto.decrypt(
            provider_id=credential.provider_id,
            credential_id=credential.id,
            version=credential.version,
            ciphertext=credential.encrypted_api_key,
        ) == secret
        assert secret not in str(audit.metadata_json)


@pytest.mark.asyncio
async def test_anthropic_key_uses_encrypted_provider_credential_storage(credential_client):
    client, factory, _, crypto = credential_client
    secret = "anthropic-key-sensitive"
    response = await client.post("/api/v1/gateway/provider-connections", json={
        "name": "anthropic-primary", "display_name": "Anthropic", "kind": "anthropic", "purpose": "target",
        "api_key": secret,
    })

    assert response.status_code == 201, response.text
    assert secret not in response.text
    assert response.json()["base_url"] == "https://api.anthropic.com"
    async with factory() as session:
        credential = await session.scalar(select(ProviderCredential))
        assert credential.encrypted_api_key != secret
        assert crypto.decrypt(provider_id=credential.provider_id, credential_id=credential.id,
            version=credential.version, ciphertext=credential.encrypted_api_key) == secret


@pytest.mark.asyncio
async def test_failed_audit_does_not_commit_provider_or_credential(credential_client, monkeypatch):
    client, factory, _, _ = credential_client
    secret = "provider-key-rolled-back"

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(AuditRepository, "add_event", fail_audit)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as failing_client:
        response = await failing_client.post("/api/v1/gateway/provider-connections", json={
            "name": "rolled-back",
            "display_name": "Rolled back",
            "kind": "openai",
            "purpose": "target",
            "api_key": secret,
        })

    assert response.status_code == 500
    assert secret not in response.text
    async with factory() as session:
        assert await session.scalar(select(ProviderCredential.id)) is None
        from src.shared.models.gateway import Provider

        assert await session.scalar(select(Provider.id)) is None


@pytest.mark.asyncio
async def test_invalid_input_does_not_echo_secret(credential_client):
    client, _, _, _ = credential_client
    secret = "raw-sensitive-input"
    response = await client.post("/api/v1/gateway/provider-connections", json={
        "name": "bad-target",
        "display_name": "Bad target",
        "kind": "typesafe",
        "purpose": "target",
        "api_key": secret,
    })

    assert response.status_code == 422
    assert secret not in response.text


@pytest.mark.asyncio
async def test_replace_expected_version_conflict(credential_client):
    client, _, _, _ = credential_client
    created = await client.post("/api/v1/gateway/provider-connections", json={
        "name": "openai-cas", "display_name": "OpenAI CAS", "kind": "openai",
        "purpose": "target", "api_key": "first-provider-key",
    })
    provider_id = created.json()["id"]

    response = await client.put(f"/api/v1/gateway/provider-connections/{provider_id}/credential", json={
        "api_key": "second-provider-key", "expected_version": 0,
    })

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_revoke_scrubs_ciphertext_and_resets_test_metadata(credential_client):
    client, factory, _, _ = credential_client
    created = await client.post("/api/v1/gateway/provider-connections", json={
        "name": "openai-revoke", "display_name": "OpenAI Revoke", "kind": "openai",
        "purpose": "target", "api_key": "provider-key-to-revoke",
    })
    provider_id = created.json()["id"]
    async with factory() as session:
        credential = await session.scalar(select(ProviderCredential))
        credential.last_test_status = "healthy"
        credential.last_test_credential_version = 1
        await session.commit()

    response = await client.delete(
        f"/api/v1/gateway/provider-connections/{provider_id}/credential?expected_version=1"
    )

    assert response.status_code == 204
    async with factory() as session:
        credential = await session.scalar(select(ProviderCredential))
        assert (credential.status, credential.encrypted_api_key, credential.key_last4, credential.version) == (
            "revoked", "", None, 2,
        )
        assert credential.last_test_status == "untested"
        assert credential.last_test_credential_version is None


@pytest.mark.asyncio
async def test_replace_resets_test_metadata(credential_client):
    client, factory, _, _ = credential_client
    created = await client.post("/api/v1/gateway/provider-connections", json={
        "name": "openai-replace", "display_name": "OpenAI Replace", "kind": "openai",
        "purpose": "target", "api_key": "provider-key-before",
    })
    provider_id = created.json()["id"]
    async with factory() as session:
        credential = await session.scalar(select(ProviderCredential))
        credential.last_test_status = "healthy"
        credential.last_test_credential_version = 1
        await session.commit()

    response = await client.put(f"/api/v1/gateway/provider-connections/{provider_id}/credential", json={
        "api_key": "provider-key-after", "expected_version": 1,
    })

    assert response.status_code == 200
    assert response.json()["credential"]["version"] == 2
    assert response.json()["credential"]["last_test_status"] == "untested"
    async with factory() as session:
        credential = await session.scalar(select(ProviderCredential))
        assert credential.last_test_credential_version is None


@pytest.mark.asyncio
async def test_internal_typesafe_hidden_from_user(credential_client):
    client, _, state, _ = credential_client
    created = await client.post("/api/v1/gateway/provider-connections", json={
        "name": "typesafe-routing", "display_name": "TypeSafe Routing", "kind": "typesafe",
        "purpose": "internal", "api_key": "typesafe-provider-key",
    })
    assert created.status_code == 201, created.text
    providers = await client.get("/api/v1/gateway/providers")
    assert providers.status_code == 200
    assert all(item["id"] != created.json()["id"] for item in providers.json())

    state["permissions"] = {"models.read_available"}
    forbidden = await client.get("/api/v1/gateway/provider-connections")
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_binding_rejects_wrong_kind(credential_client):
    client, _, _, _ = credential_client
    google = await client.post("/api/v1/gateway/provider-connections", json={
        "name": "google-ocr", "display_name": "Google OCR", "kind": "google",
        "purpose": "internal", "api_key": "google-provider-key",
    })

    response = await client.put("/api/v1/gateway/service-provider-bindings/routing_jev", json={
        "provider_id": google.json()["id"],
    })

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_endpoint_rejects_noncanonical_base_url(credential_client):
    client, _, _, _ = credential_client

    response = await client.post("/api/v1/gateway/provider-connections", json={
        "name": "custom-openai", "display_name": "Custom OpenAI", "kind": "openai",
        "purpose": "target", "base_url": "https://example.com/v1", "api_key": "provider-key",
    })

    assert response.status_code == 422


def test_anthropic_target_provider_is_valid_without_a_key():
    from datetime import UTC, datetime

    payload = ProviderConnectionCreate.model_validate({
        "name": "anthropic",
        "display_name": "Anthropic",
        "kind": "anthropic",
        "purpose": "target",
    })

    ProviderConnectionService._validate_kind_purpose(payload.kind, payload.purpose)
    assert ProviderConnectionService._canonical_base_url(payload.kind, None) == "https://api.anthropic.com"
    assert payload.api_key is None

    from src.shared.models.gateway import Provider

    read = ProviderConnectionService._read(
        Provider(
            id="anthropic-1", name=payload.name, display_name=payload.display_name, kind=payload.kind,
            purpose=payload.purpose, status=payload.status, base_url="https://api.anthropic.com",
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        ),
        None,
    )
    assert read.credential.configured is False
    assert read.base_url == "https://api.anthropic.com"


def test_anthropic_cannot_be_used_as_an_internal_provider():
    with pytest.raises(ProviderConnectionInvalidError):
        ProviderConnectionService._validate_kind_purpose("anthropic", "internal")
