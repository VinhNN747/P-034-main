import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.models.gateway import Provider
from src.shared.models.provider_credentials import ProviderCredential, ServiceProviderBinding
from src.shared.services.credential_crypto import CredentialCrypto
from src.shared.services.provider_credential_resolver import (
    CredentialUnavailableError,
    ProviderCredentialResolver,
)


@pytest_asyncio.fixture
async def credential_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def crypto_for_test():
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        credential_encryption_key=Fernet.generate_key().decode(),
    )
    return CredentialCrypto.from_settings(settings)


async def add_connection(session, crypto, *, provider_id, kind, key, purpose="target", status="active"):
    provider = Provider(
        id=provider_id,
        name=provider_id,
        display_name=provider_id,
        kind=kind,
        purpose=purpose,
        status="active",
    )
    credential_id = f"credential-{provider_id}"
    credential = ProviderCredential(
        id=credential_id,
        provider_id=provider_id,
        encrypted_api_key=crypto.encrypt(
            provider_id=provider_id,
            credential_id=credential_id,
            version=1,
            api_key=key,
        ),
        version=1,
        status=status,
    )
    session.add_all([provider, credential])
    return provider, credential


@pytest.mark.asyncio
async def test_two_openai_connections_resolve_distinct_keys(credential_session):
    crypto = crypto_for_test()
    await add_connection(credential_session, crypto, provider_id="openai-a", kind="openai", key="key-a")
    await add_connection(credential_session, crypto, provider_id="openai-b", kind="openai", key="key-b")
    await credential_session.commit()

    resolver = ProviderCredentialResolver(credential_session, crypto)
    first = await resolver.resolve_provider("openai-a")
    second = await resolver.resolve_provider("openai-b")

    assert (first.provider_id, first.api_key) == ("openai-a", "key-a")
    assert (second.provider_id, second.api_key) == ("openai-b", "key-b")
    assert "key-a" not in repr(first)


@pytest.mark.asyncio
async def test_binding_kind_validation(credential_session):
    crypto = crypto_for_test()
    await add_connection(credential_session, crypto, provider_id="google", kind="google", key="google-key", purpose="internal")
    credential_session.add(ServiceProviderBinding(purpose="routing_jev", provider_id="google"))
    await credential_session.commit()

    with pytest.raises(CredentialUnavailableError):
        await ProviderCredentialResolver(credential_session, crypto).resolve_service("routing_jev")


@pytest.mark.asyncio
async def test_revoked_never_decrypts(credential_session):
    crypto = crypto_for_test()
    _, credential = await add_connection(
        credential_session, crypto, provider_id="revoked", kind="openai", key="revoked-key", status="revoked"
    )
    credential.encrypted_api_key = "not-valid-ciphertext"
    await credential_session.commit()

    with pytest.raises(CredentialUnavailableError):
        await ProviderCredentialResolver(credential_session, crypto).resolve_provider("revoked")


@pytest.mark.asyncio
async def test_missing_or_wrong_master_key_fails_closed(credential_session):
    writer = crypto_for_test()
    await add_connection(credential_session, writer, provider_id="openai", kind="openai", key="key")
    await credential_session.commit()

    with pytest.raises(CredentialUnavailableError):
        await ProviderCredentialResolver(credential_session, None).resolve_provider("openai")
    with pytest.raises(CredentialUnavailableError):
        await ProviderCredentialResolver(credential_session, crypto_for_test()).resolve_provider("openai")


@pytest.mark.asyncio
async def test_configured_provider_ids_uses_active_credential_presence_only(credential_session):
    crypto = crypto_for_test()
    await add_connection(credential_session, crypto, provider_id="configured", kind="openai", key="key")
    await add_connection(credential_session, crypto, provider_id="revoked", kind="openai", key="key", status="revoked")
    await credential_session.commit()

    ids = await ProviderCredentialResolver(credential_session, crypto).configured_provider_ids()
    assert ids == {"configured"}
