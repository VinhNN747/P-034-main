
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.shared.database.base import Base
from src.shared.models.gateway import Provider
from src.shared.models.provider_credentials import ProviderCredential, ServiceProviderBinding
from src.shared.services.credential_crypto import CredentialCrypto
from src.shared.services.provider_credential_resolver import ProviderCredentialResolver


@pytest_asyncio.fixture
async def import_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def crypto():
    from src.shared.config import Settings

    return CredentialCrypto.from_settings(
        Settings(_env_file=None, credential_encryption_key=Fernet.generate_key().decode())
    )


async def add_provider(session, *, name, kind, env_var, purpose="target"):
    provider = Provider(
        id=name,
        name=name,
        display_name=name,
        kind=kind,
        purpose=purpose,
        status="active",
        secret_env_var=env_var,
    )
    session.add(provider)
    await session.flush()
    return provider


@pytest.mark.asyncio
async def test_dry_run_has_no_writes(import_session, crypto):
    from src.scripts.import_provider_credentials import import_credentials

    await add_provider(import_session, name="openai", kind="openai", env_var="OPENAI_API_KEY")
    summary = await import_credentials(
        import_session, crypto, {"OPENAI_API_KEY": "openai-secret-value"}, apply=False
    )

    assert summary.created_credentials == 1
    assert await import_session.scalar(select(ProviderCredential.id)) is None
    assert await import_session.scalar(select(ServiceProviderBinding.purpose)) is None


@pytest.mark.asyncio
async def test_second_import_skips_existing_credentials(import_session, crypto):
    from src.scripts.import_provider_credentials import import_credentials

    await add_provider(import_session, name="openai", kind="openai", env_var="OPENAI_API_KEY")
    values = {"OPENAI_API_KEY": "openai-secret-value"}
    await import_credentials(import_session, crypto, values, apply=True)
    credential = await import_session.scalar(select(ProviderCredential))
    original_id = credential.id
    original_ciphertext = credential.encrypted_api_key

    second = await import_credentials(import_session, crypto, values, apply=True)

    assert second.skipped_credentials == 1
    assert await import_session.scalar(select(ProviderCredential.id)) == original_id
    assert credential.encrypted_api_key == original_ciphertext
    resolved = await ProviderCredentialResolver(import_session, crypto).resolve_provider("openai")
    assert resolved.api_key == values["OPENAI_API_KEY"]


@pytest.mark.asyncio
async def test_seeded_openai_uses_default_legacy_mapping(import_session, crypto):
    from src.scripts.import_provider_credentials import import_credentials

    await add_provider(import_session, name="openai", kind="openai", env_var="")

    summary = await import_credentials(
        import_session, crypto, {"OPENAI_API_KEY": "openai-secret-value"}, apply=True
    )

    assert summary.created_credentials == 1
    assert (await ProviderCredentialResolver(import_session, crypto).resolve_provider("openai")).api_key == (
        "openai-secret-value"
    )


@pytest.mark.asyncio
async def test_google_alias_conflict_is_reported_without_secret(import_session, crypto):
    from src.scripts.import_provider_credentials import import_credentials

    await add_provider(import_session, name="google", kind="google", env_var="GOOGLE_API_KEY")
    values = {
        "GOOGLE_API_KEY": "google-secret-value",
        "GEMINI_API_KEY": "gemini-secret-value",
    }

    summary = await import_credentials(import_session, crypto, values, apply=False)

    assert summary.warnings
    assert all(secret not in " ".join(summary.warnings) for secret in values.values())
    assert "GEMINI_API_KEY" in " ".join(summary.warnings)
    assert await import_session.scalar(select(ProviderCredential.id)) is None


@pytest.mark.asyncio
async def test_google_ocr_distinct_key_preserved(import_session, crypto):
    from src.scripts.import_provider_credentials import import_credentials

    await add_provider(import_session, name="google", kind="google", env_var="GOOGLE_API_KEY")
    await import_credentials(
        import_session,
        crypto,
        {"GOOGLE_API_KEY": "google-ocr-secret", "GEMINI_API_KEY": "gemini-chat-secret"},
        apply=True,
    )

    binding = await import_session.get(ServiceProviderBinding, "ocr_google")
    assert binding is not None
    resolved = await ProviderCredentialResolver(import_session, crypto).resolve_service("ocr_google")
    target = await ProviderCredentialResolver(import_session, crypto).resolve_provider("google")
    assert resolved.api_key == "google-ocr-secret"
    assert resolved.provider_name == "google-ocr-legacy"
    assert target.api_key == "gemini-chat-secret"


@pytest.mark.asyncio
async def test_failed_import_rolls_back_all(import_session, crypto):
    from src.scripts.import_provider_credentials import import_credentials

    await add_provider(import_session, name="a-openai", kind="openai", env_var="OPENAI_API_KEY")
    await add_provider(import_session, name="b-google", kind="google", env_var="GEMINI_API_KEY")

    class FailingCrypto:
        def __init__(self):
            self.calls = 0

        def encrypt(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("encryption failed")
            return crypto.encrypt(**kwargs)

    with pytest.raises(RuntimeError, match="encryption failed"):
        await import_credentials(
            import_session,
            FailingCrypto(),
            {"OPENAI_API_KEY": "openai-secret-value", "GEMINI_API_KEY": "gemini-secret-value"},
            apply=True,
        )

    assert (await import_session.scalars(select(ProviderCredential))).all() == []
    assert (await import_session.scalars(select(ServiceProviderBinding))).all() == []


@pytest.mark.asyncio
async def test_missing_binding_created_without_overwriting_existing(import_session, crypto):
    from src.scripts.import_provider_credentials import import_credentials

    await add_provider(import_session, name="openai", kind="openai", env_var="OPENAI_API_KEY")
    await add_provider(import_session, name="manual-openai", kind="openai", env_var="", purpose="internal")
    import_session.add(ServiceProviderBinding(purpose="internal_openai", provider_id="manual-openai"))
    await import_session.commit()

    await import_credentials(
        import_session, crypto, {"OPENAI_API_KEY": "openai-secret-value"}, apply=True
    )

    binding = await import_session.get(ServiceProviderBinding, "internal_openai")
    assert binding.provider_id == "manual-openai"


@pytest.mark.asyncio
async def test_seed_never_overwrites_credentials(import_session, crypto):
    from src.scripts.seed_backend import seed_session

    await seed_session(import_session)
    provider = await import_session.scalar(select(Provider).where(Provider.kind == "openai"))
    assert provider is not None
    credential_id = "seed-test-credential"
    expected_key = "preserve-existing-key"
    credential = ProviderCredential(
        id=credential_id,
        provider_id=provider.id,
        encrypted_api_key=crypto.encrypt(
            provider_id=provider.id,
            credential_id=credential_id,
            version=1,
            api_key=expected_key,
        ),
        version=1,
        status="active",
    )
    import_session.add(credential)
    await import_session.commit()
    ciphertext = credential.encrypted_api_key

    await seed_session(import_session)

    persisted = await import_session.get(ProviderCredential, credential_id)
    assert persisted.encrypted_api_key == ciphertext
    assert "OPENAI_API_KEY" not in provider.secret_env_var
