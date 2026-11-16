from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.models.gateway import Model, Provider
from src.shared.models.provider_credentials import ProviderCredential
from src.shared.services.credential_crypto import CredentialCrypto
from src.shared.services.provider_credential_resolver import ProviderCredentialResolver


@pytest_asyncio.fixture
async def provider_builder_context(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'client-builder.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        gateway_execution_mode="live",
        gateway_openai_enabled=True,
        gateway_gemini_enabled=True,
        gateway_openrouter_enabled=True,
        credential_encryption_key=key,
    )
    crypto = CredentialCrypto.from_settings(settings)
    async with factory() as session:
        yield session, settings, crypto
    await engine.dispose()


async def add_openai_connection(session, crypto, *, provider_id, provider_name, model_id, model_name, api_key):
    provider = Provider(
        id=provider_id,
        name=provider_name,
        display_name=provider_name,
        kind="openai",
        purpose="target",
        status="active",
        base_url="https://api.openai.com/v1",
    )
    model = Model(
        id=model_id,
        provider_id=provider_id,
        name=model_name,
        display_name=model_name,
        status="active",
        capabilities_json=["chat"],
    )
    credential_id = f"credential-{provider_id}"
    credential = ProviderCredential(
        id=credential_id,
        provider_id=provider_id,
        encrypted_api_key=crypto.encrypt(
            provider_id=provider_id,
            credential_id=credential_id,
            version=1,
            api_key=api_key,
        ),
        version=1,
        status="active",
    )
    session.add_all([provider, model, credential])
    return model, provider, credential


@pytest.mark.asyncio
async def test_same_kind_connections_do_not_share_credentials(provider_builder_context):
    from src.backend_ai.gateway.providers.openai import OpenAIProvider
    from src.backend_ai.services.provider_client_builder import ProviderClientBuilder

    session, settings, crypto = provider_builder_context
    first = await add_openai_connection(
        session,
        crypto,
        provider_id="provider-a",
        provider_name="openai-a",
        model_id="model-a",
        model_name="gpt-shared",
        api_key="key-for-a",
    )
    second = await add_openai_connection(
        session,
        crypto,
        provider_id="provider-b",
        provider_name="openai-b",
        model_id="model-b",
        model_name="gpt-shared",
        api_key="key-for-b",
    )
    await session.commit()

    builder = ProviderClientBuilder(ProviderCredentialResolver(session, crypto), settings)
    clients = await builder.build_for_models([(first[0], first[1]), (second[0], second[1])])

    assert isinstance(clients["model-a"], OpenAIProvider)
    assert isinstance(clients["model-b"], OpenAIProvider)
    assert clients["model-a"]._api_key == "key-for-a"
    assert clients["model-b"]._api_key == "key-for-b"
    assert "gpt-shared" not in clients
    assert "openai" not in clients


@pytest.mark.asyncio
async def test_missing_primary_connection_keeps_other_connection_available(provider_builder_context):
    from src.backend_ai.services.provider_client_builder import ProviderClientBuilder

    session, settings, crypto = provider_builder_context
    primary = await add_openai_connection(
        session,
        crypto,
        provider_id="provider-primary",
        provider_name="primary",
        model_id="model-primary",
        model_name="gpt-primary",
        api_key="primary-key",
    )
    fallback = await add_openai_connection(
        session,
        crypto,
        provider_id="provider-fallback",
        provider_name="fallback",
        model_id="model-fallback",
        model_name="gpt-fallback",
        api_key="fallback-key",
    )
    primary[2].status = "revoked"
    await session.commit()

    builder = ProviderClientBuilder(ProviderCredentialResolver(session, crypto), settings)
    clients = await builder.build_for_models([(primary[0], primary[1]), (fallback[0], fallback[1])])

    assert "model-primary" not in clients
    assert "model-fallback" in clients
    assert builder.available_provider_ids == {"provider-fallback"}


@pytest.mark.asyncio
async def test_wrong_key_connection_does_not_disable_other_connection(provider_builder_context):
    from src.backend_ai.services.provider_client_builder import ProviderClientBuilder

    session, settings, crypto = provider_builder_context
    bad = await add_openai_connection(
        session,
        crypto,
        provider_id="provider-bad",
        provider_name="bad",
        model_id="model-bad",
        model_name="gpt-bad",
        api_key="bad-key",
    )
    good = await add_openai_connection(
        session,
        crypto,
        provider_id="provider-good",
        provider_name="good",
        model_id="model-good",
        model_name="gpt-good",
        api_key="good-key",
    )
    bad[2].encrypted_api_key = "not-a-valid-token"
    await session.commit()

    builder = ProviderClientBuilder(ProviderCredentialResolver(session, crypto), settings)
    clients = await builder.build_for_models([(bad[0], bad[1]), (good[0], good[1])])

    assert "model-bad" not in clients
    assert clients["model-good"]._api_key == "good-key"


@pytest.mark.asyncio
async def test_live_builder_ignores_legacy_environment_key(provider_builder_context, monkeypatch):
    from src.backend_ai.services.provider_client_builder import ProviderClientBuilder

    session, settings, crypto = provider_builder_context
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-env-must-not-be-used")
    builder = ProviderClientBuilder(ProviderCredentialResolver(session, crypto), settings)

    clients = await builder.build_for_models([])

    assert clients == {}
    assert builder.available_provider_ids == set()


@pytest.mark.asyncio
async def test_new_build_reads_replaced_credential_version(provider_builder_context):
    from src.backend_ai.services.provider_client_builder import ProviderClientBuilder

    session, settings, crypto = provider_builder_context
    model, provider, credential = await add_openai_connection(
        session,
        crypto,
        provider_id="provider-rotate",
        provider_name="rotate",
        model_id="model-rotate",
        model_name="gpt-rotate",
        api_key="first-key",
    )
    await session.commit()
    resolver = ProviderCredentialResolver(session, crypto)
    builder = ProviderClientBuilder(resolver, settings)
    first = await builder.build_for_models([(model, provider)])
    credential.version = 2
    credential.encrypted_api_key = crypto.encrypt(
        provider_id=provider.id,
        credential_id=credential.id,
        version=2,
        api_key="second-key",
    )
    await session.commit()

    second = await builder.build_for_models([(model, provider)])

    assert first[model.id]._api_key == "first-key"
    assert second[model.id]._api_key == "second-key"
    assert first[model.id] is not second[model.id]


def test_pinned_duplicate_name_returns_ambiguity():
    from src.backend_ai.services.gateway_execution_service import GatewayExecutionService

    rows = [
        (None, Model(id="a", provider_id="p1", name="gpt-shared", display_name="A"), None),
        (None, Model(id="b", provider_id="p2", name="gpt-shared", display_name="B"), None),
    ]

    with pytest.raises(ValueError, match="ambiguous"):
        GatewayExecutionService._ensure_unambiguous_model_pins(rows, {"gpt-shared"})


@pytest.mark.asyncio
async def test_anthropic_builder_passes_model_profile_from_catalog(monkeypatch):
    from types import SimpleNamespace

    from src.backend_ai.services import provider_client_builder as module
    from src.backend_ai.services.provider_client_builder import ProviderClientBuilder

    settings = Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:",
        gateway_execution_mode="live", gateway_anthropic_enabled=True)
    model = Model(id="anthropic-model-id", provider_id="anthropic-provider-id", name="claude-haiku-5-5",
        display_name="Claude Haiku 5.5", status="active", capabilities_json=["chat"], max_output_tokens=128000,
        feature_metadata_json={"constraints": {"thinking_mode": "adaptive", "default_effort": "medium"}})
    provider = Provider(id=model.provider_id, name="anthropic", display_name="Anthropic", kind="anthropic",
        purpose="target", status="active", base_url="https://api.anthropic.com")
    constructed = []

    class FakeAnthropicProvider:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

    class Resolver:
        async def resolve_provider(self, provider_id):
            assert provider_id == provider.id
            return SimpleNamespace(api_key="database-key", version=6)

    monkeypatch.setattr(module, "AnthropicProvider", FakeAnthropicProvider, raising=False)
    builder = ProviderClientBuilder(Resolver(), settings)

    clients = await builder.build_for_models([(model, provider)])

    assert isinstance(clients[model.id], FakeAnthropicProvider)
    assert builder.credential_versions[provider.id] == 6
    assert constructed == [{"api_key": "database-key", "base_url": "https://api.anthropic.com", "enabled": True,
        "model_profiles": {
            model.name: {"max_output_tokens": 128000, "feature_metadata_json": model.feature_metadata_json},
            model.id: {"max_output_tokens": 128000, "feature_metadata_json": model.feature_metadata_json},
        }}]


@pytest.mark.asyncio
async def test_anthropic_profile_remains_readable_after_orm_model_expires(provider_builder_context):
    from src.backend_ai.gateway.providers.anthropic import AnthropicProvider
    from src.backend_ai.services.provider_client_builder import ProviderClientBuilder

    session, settings, crypto = provider_builder_context
    settings.gateway_anthropic_enabled = True
    provider = Provider(id="anthropic-provider", name="anthropic", display_name="Anthropic", kind="anthropic",
        purpose="target", status="active", base_url="https://api.anthropic.com")
    model = Model(id="anthropic-model", provider_id=provider.id, name="claude-haiku-5-5",
        display_name="Claude Haiku 5.5", status="active", capabilities_json=["chat"], max_output_tokens=128000,
        feature_metadata_json={"constraints": {"thinking_mode": "adaptive", "default_effort": "medium"}})
    credential_id = "credential-anthropic"
    credential = ProviderCredential(id=credential_id, provider_id=provider.id,
        encrypted_api_key=crypto.encrypt(provider_id=provider.id, credential_id=credential_id, version=1,
            api_key="anthropic-test-key"), version=1, status="active")
    session.add_all([provider, model, credential])
    await session.commit()

    builder = ProviderClientBuilder(ProviderCredentialResolver(session, crypto), settings)
    clients = await builder.build_for_models([(model, provider)])
    client = clients[model.id]
    assert isinstance(client, AnthropicProvider)

    session.expire(model)
    request = client._request("hello", "claude-haiku-5-5", None)

    assert request["max_tokens"] == 128000
    assert request["output_config"] == {"effort": "medium"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, enabled, has_credential", [
    ("live", False, True), ("offline", True, True), ("live", True, False),
])
async def test_anthropic_builder_is_not_callable_offline_disabled_or_without_key(mode, enabled, has_credential,
                                                                                 monkeypatch):
    from types import SimpleNamespace

    from src.backend_ai.services import provider_client_builder as module
    from src.backend_ai.services.provider_client_builder import ProviderClientBuilder
    from src.shared.services.provider_credential_resolver import CredentialUnavailableError

    settings = Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:",
        gateway_execution_mode=mode, gateway_anthropic_enabled=enabled)
    model = Model(id="anthropic-model-id", provider_id="anthropic-provider-id", name="claude-haiku-5-5",
        display_name="Claude Haiku 5.5", status="active", capabilities_json=["chat"], max_output_tokens=128000,
        feature_metadata_json={"constraints": {"thinking_mode": "adaptive", "default_effort": "medium"}})
    provider = Provider(id=model.provider_id, name="anthropic", display_name="Anthropic", kind="anthropic",
        purpose="target", status="active", base_url="https://api.anthropic.com")
    constructed = []

    class FakeAnthropicProvider:
        def __init__(self, **_kwargs):
            constructed.append(True)

    class Resolver:
        async def resolve_provider(self, _provider_id):
            if not has_credential:
                raise CredentialUnavailableError()
            return SimpleNamespace(api_key="database-key", version=1)

    monkeypatch.setattr(module, "AnthropicProvider", FakeAnthropicProvider, raising=False)
    clients = await ProviderClientBuilder(Resolver(), settings).build_for_models([(model, provider)])

    assert clients == {}
    assert constructed == []
