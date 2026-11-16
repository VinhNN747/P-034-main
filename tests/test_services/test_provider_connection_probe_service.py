from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.backend_ai.services import provider_connection_probe_service as probe_module
from src.backend_ai.services.provider_connection_probe_service import (
    ProviderConnectionProbeService,
    ProviderProbeConflictError,
    ProviderProbeInvalidError,
    _sanitize_provider_error,
)
from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.dtos.provider_connection_probe import ConnectionProbeInput
from src.shared.models.audit import AuditEventRecord
from src.shared.models.gateway import Provider
from src.shared.models.provider_credentials import ProviderCredential
from src.shared.services.credential_crypto import CredentialCrypto
from src.shared.services.provider_credential_resolver import ProviderCredentialResolver


@pytest_asyncio.fixture
async def probe_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'provider-probes.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session, factory
    await engine.dispose()


def _crypto():
    return CredentialCrypto.from_settings(
        Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///:memory:",
            credential_encryption_key=Fernet.generate_key().decode(),
        )
    )


def _settings(**kwargs):
    values = {
        "gateway_execution_mode": "live",
        "gateway_openai_enabled": True,
        "gateway_gemini_enabled": True,
        "gateway_ocr_enabled": True,
        "gateway_jev_enabled": True,
        **kwargs,
    }
    return Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", **values)


async def _add_provider(session, crypto, provider_id, kind="openai", *, purpose="target", status="active", key="key-a"):
    provider = Provider(
        id=provider_id,
        name=provider_id,
        display_name=provider_id,
        kind=kind,
        purpose=purpose,
        status=status,
        base_url="https://api.openai.com/v1" if kind == "openai" else None,
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
        status="active",
    )
    session.add_all([provider, credential])
    await session.commit()
    return provider, credential


def _service(session, crypto, *, settings=None, actor="admin-1"):
    resolver = ProviderCredentialResolver(session, crypto)
    return ProviderConnectionProbeService(
        session,
        resolver,
        settings or _settings(),
        actor_user_id=actor,
    )


@pytest.mark.asyncio
async def test_two_same_kind_connections_probe_their_own_keys(probe_db, monkeypatch):
    session, _ = probe_db
    crypto = _crypto()
    await _add_provider(session, crypto, "openai-one", key="secret-one")
    await _add_provider(session, crypto, "openai-two", key="secret-two")
    received = []

    async def fake_call(self, kind, base_url, api_key, payload):
        received.append((kind, base_url, api_key, payload.model_name))

    monkeypatch.setattr(ProviderConnectionProbeService, "_call_upstream", fake_call)
    service = _service(session, crypto)
    first = await service.probe("openai-one", ConnectionProbeInput(expected_version=1, mode="chat", model_name="gpt-test"))
    second = await service.probe("openai-two", ConnectionProbeInput(expected_version=1, mode="chat", model_name="gpt-test"))

    assert [row[2] for row in received] == ["secret-one", "secret-two"]
    assert first.status == second.status == "healthy"
    assert first.model_name == "gpt-test"
    assert "secret-one" not in first.model_dump_json()


@pytest.mark.asyncio
async def test_chat_probe_without_model_uses_the_configured_default(probe_db, monkeypatch):
    session, _ = probe_db
    crypto = _crypto()
    provider, _ = await _add_provider(session, crypto, "openai-default-model")
    received = []

    async def fake_call(self, kind, base_url, api_key, payload):
        received.append((kind, payload.model_name))

    monkeypatch.setattr(ProviderConnectionProbeService, "_call_upstream", fake_call)
    result = await _service(session, crypto, settings=_settings(model_name="configured-chat-model")).probe(
        provider.id,
        ConnectionProbeInput(expected_version=1, mode="chat"),
    )

    assert received == [("openai", "configured-chat-model")]
    assert result.status == "healthy"
    assert result.model_name == "configured-chat-model"


@pytest.mark.asyncio
async def test_jev_probe_without_openai_or_catalog_model(probe_db, monkeypatch):
    session, _ = probe_db
    crypto = _crypto()
    await _add_provider(session, crypto, "jev-internal", "typesafe", purpose="internal", key="jev-secret")
    received = []

    async def fake_call(self, kind, base_url, api_key, payload):
        received.append((kind, api_key, payload.mode, payload.model_name))

    monkeypatch.setattr(ProviderConnectionProbeService, "_call_upstream", fake_call)
    result = await _service(session, crypto).probe(
        "jev-internal", ConnectionProbeInput(expected_version=1, mode="jev")
    )

    assert received == [("typesafe", "jev-secret", "jev", "jev-latest")]
    assert result.status == "healthy"
    assert await session.scalar(select(Provider.id)) == "jev-internal"
    assert await session.scalar(select(ProviderCredential.provider_id)) == "jev-internal"


@pytest.mark.asyncio
async def test_probe_can_test_inactive_connection_without_activating(probe_db, monkeypatch):
    session, _ = probe_db
    crypto = _crypto()
    provider, _ = await _add_provider(session, crypto, "inactive", status="inactive")
    monkeypatch.setattr(ProviderConnectionProbeService, "_call_upstream", lambda *args: _no_op())

    result = await _service(session, crypto).probe(
        provider.id, ConnectionProbeInput(expected_version=1, mode="chat", model_name="gpt-test")
    )

    assert result.status == "healthy"
    await session.refresh(provider)
    assert provider.status == "inactive"


async def _no_op():
    return None


@pytest.mark.asyncio
async def test_offline_skips_without_upstream_call_or_fake_credential(probe_db, monkeypatch):
    session, _ = probe_db
    provider = Provider(
        id="offline",
        name="offline",
        display_name="Offline",
        kind="offline",
        purpose="target",
        status="active",
    )
    session.add(provider)
    await session.commit()
    called = False

    async def fail_if_called(*args):
        nonlocal called
        called = True
        raise AssertionError("offline connection must not call upstream")

    monkeypatch.setattr(ProviderConnectionProbeService, "_call_upstream", fail_if_called)
    result = await _service(session, None, settings=_settings(gateway_execution_mode="offline")).probe(
        "offline", ConnectionProbeInput(expected_version=0, mode="chat")
    )

    assert result.status == "skipped"
    assert result.credential_version is None
    assert not called
    assert await session.scalar(select(ProviderCredential.id)) is None


@pytest.mark.asyncio
async def test_live_connection_is_skipped_while_global_execution_is_offline(probe_db, monkeypatch):
    session, _ = probe_db
    crypto = _crypto()
    provider, credential = await _add_provider(session, crypto, "live-but-offline-mode")

    async def fail_if_called(*args):
        raise AssertionError("global offline mode must not call upstream")

    monkeypatch.setattr(ProviderConnectionProbeService, "_call_upstream", fail_if_called)
    result = await _service(
        session,
        crypto,
        settings=_settings(gateway_execution_mode="offline"),
    ).probe(provider.id, ConnectionProbeInput(expected_version=1, mode="chat"))

    assert result.status == "skipped"
    assert result.credential_version == 1
    await session.refresh(credential)
    assert credential.last_test_status == "skipped"
    assert credential.last_test_credential_version == 1


@pytest.mark.asyncio
async def test_revoked_credential_is_unavailable_and_never_called(probe_db, monkeypatch):
    session, _ = probe_db
    crypto = _crypto()
    provider, credential = await _add_provider(session, crypto, "revoked")
    credential.status = "revoked"
    credential.encrypted_api_key = ""
    await session.commit()
    calls = []

    async def fake_call(*args):
        calls.append(args)

    monkeypatch.setattr(ProviderConnectionProbeService, "_call_upstream", fake_call)
    result = await _service(session, crypto).probe(
        provider.id, ConnectionProbeInput(expected_version=1, mode="chat", model_name="gpt-test")
    )

    assert result.status == "unavailable"
    assert result.error_code == "CREDENTIAL_UNAVAILABLE"
    assert calls == []


@pytest.mark.asyncio
async def test_key_changed_during_probe_discards_stale_result(probe_db, monkeypatch):
    session, factory = probe_db
    crypto = _crypto()
    provider, credential = await _add_provider(session, crypto, "changing")
    provider_id = provider.id
    credential_id = credential.id

    async def rotate_while_network_call_runs(self, kind, base_url, api_key, payload):
        assert api_key == "key-a"
        async with factory() as other:
            current = await other.get(ProviderCredential, credential_id)
            current.version = 2
            current.encrypted_api_key = crypto.encrypt(
                provider_id=provider_id,
                credential_id=credential_id,
                version=2,
                api_key="rotated-key",
            )
            await other.commit()

    monkeypatch.setattr(ProviderConnectionProbeService, "_call_upstream", rotate_while_network_call_runs)
    with pytest.raises(ProviderProbeConflictError):
        await _service(session, crypto).probe(
            provider_id, ConnectionProbeInput(expected_version=1, mode="chat", model_name="gpt-test")
        )

    async with factory() as check:
        current = await check.get(ProviderCredential, credential_id)
        assert current.version == 2
        assert current.last_test_credential_version is None
        assert await check.scalar(select(AuditEventRecord.id)) is None


class StatusError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (StatusError(401), "AUTHENTICATION_FAILED"),
        (StatusError(403), "ACCESS_DENIED"),
        (StatusError(402), "QUOTA_EXCEEDED"),
        (StatusError(429), "RATE_LIMITED"),
        (StatusError(404), "MODEL_UNAVAILABLE"),
        (TimeoutError("sensitive-timeout"), "TIMEOUT"),
        (StatusError(503), "PROVIDER_ERROR"),
    ],
)
def test_provider_errors_have_distinct_sanitized_codes(exception, expected):
    code, message = _sanitize_provider_error(exception)
    assert code == expected
    assert "sensitive" not in message


def test_probe_input_rejects_a_raw_api_key():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ConnectionProbeInput.model_validate(
            {"expected_version": 1, "mode": "chat", "model_name": "gpt", "api_key": "raw-secret"}
        )


@pytest.mark.asyncio
async def test_probe_mode_dispatch_uses_fixed_timeout_and_output_cap(monkeypatch):
    calls = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls.append(("openai", kwargs))

        async def generate(self, prompt, **kwargs):
            calls.append(("chat", prompt, kwargs))

    monkeypatch.setattr(probe_module, "OpenAIProvider", FakeOpenAI)
    service = object.__new__(ProviderConnectionProbeService)
    service.settings = _settings()
    await service._call_upstream(
        "openai", "https://api.openai.com/v1", "key", ConnectionProbeInput(expected_version=1, mode="chat", model_name="gpt-x")
    )
    assert calls[0] == ("openai", {"enabled": True, "api_key": "key", "base_url": "https://api.openai.com/v1"})
    assert calls[1][1] == "Reply OK."
    assert calls[1][2]["timeout_seconds"] == 10
    assert calls[1][2]["options"].hard_completion_token_limit == 64


@pytest.mark.asyncio
async def test_chat_embedding_ocr_and_jev_modes_dispatch_to_their_own_clients(monkeypatch):
    calls = []

    class FakeEmbedding:
        def __init__(self, **kwargs):
            calls.append(("embedding-client", kwargs))

        async def probe(self, model, *, timeout_seconds, input_text):
            calls.append(("embedding", model, timeout_seconds, input_text))

    class FakeOcr:
        def __init__(self, api_key, **kwargs):
            calls.append(("ocr-client", api_key, kwargs))

        async def ocr(self, data, mime_type, *, kind):
            calls.append(("ocr", len(data), mime_type, kind))

    class FakeJev:
        def __init__(self, **kwargs):
            calls.append(("jev-client", kwargs))

        async def decide(self, prompt):
            calls.append(("jev", prompt))

    class FakeGemini:
        def __init__(self, **kwargs):
            calls.append(("google-chat-client", kwargs))

        async def generate(self, prompt, **kwargs):
            calls.append(("google-chat", prompt, kwargs))

    monkeypatch.setattr(probe_module, "OpenAIEmbeddingProvider", FakeEmbedding)
    monkeypatch.setattr(probe_module, "GeminiOcrClient", FakeOcr)
    monkeypatch.setattr(probe_module, "TypeSafeJevClient", FakeJev)
    monkeypatch.setattr(probe_module, "GeminiProvider", FakeGemini)
    service = object.__new__(ProviderConnectionProbeService)
    service.settings = _settings()

    await service._call_upstream(
        "openai", "https://api.openai.com/v1", "openai-key", ConnectionProbeInput(expected_version=1, mode="embedding")
    )
    await service._call_upstream(
        "google", None, "google-key", ConnectionProbeInput(expected_version=1, mode="ocr")
    )
    await service._call_upstream(
        "typesafe", None, "jev-key", ConnectionProbeInput(expected_version=1, mode="jev")
    )
    await service._call_upstream(
        "google", None, "google-key", ConnectionProbeInput(expected_version=1, mode="chat", model_name="gemini-test")
    )

    assert ("embedding", "text-embedding-3-small", 10, "connection test") in calls
    assert any(item[0] == "ocr" and item[2] == "image/png" for item in calls)
    assert any(item[0] == "jev" and item[1] == probe_module._PROBE_PROMPT for item in calls)
    assert any(item[0] == "google-chat" and item[1] == "Reply OK." for item in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(("visible_text", "expected_status"), [("OK", "healthy"), ("", "failed")])
async def test_anthropic_connection_probe_uses_database_profile_and_default_model(
    monkeypatch, visible_text, expected_status
):
    from types import SimpleNamespace

    from src.shared.models.gateway import Model

    provider = SimpleNamespace(id="anthropic-provider", kind="anthropic", purpose="target", status="active",
        base_url="https://api.anthropic.com")
    credential = SimpleNamespace(version=1, status="active", encrypted_api_key="ciphertext")
    model = Model(id="anthropic-haiku", provider_id=provider.id, name="claude-haiku-5-5",
        display_name="Claude Haiku 5.5", status="inactive", max_output_tokens=128_000,
        feature_metadata_json={"constraints": {"thinking_mode": "adaptive", "default_effort": "medium",
            "forced_tool_choice": "conditional"}})
    received = []

    class FakeAnthropicProvider:
        def __init__(self, **kwargs):
            received.append(("client", kwargs))

        async def generate(self, prompt, **kwargs):
            received.append(("generate", prompt, kwargs))
            return SimpleNamespace(text=visible_text)

    monkeypatch.setattr(probe_module, "AnthropicProvider", FakeAnthropicProvider, raising=False)

    class Session:
        async def scalar(self, _statement):
            return model

        def in_transaction(self):
            return False

        async def commit(self):
            return None

    class Resolver:
        async def resolve_provider_for_probe(self, _provider_id, *, expected_version):
            assert expected_version == 1
            return SimpleNamespace(api_key="key-a", version=1)

    class Repository:
        async def provider_with_credential(self, _provider_id):
            return provider, credential

        async def record_probe_if_current(self, _provider_id, version, _result):
            return version == credential.version

    class Audit:
        async def add_event(self, **_kwargs):
            return None

    service = ProviderConnectionProbeService.__new__(ProviderConnectionProbeService)
    service.session = Session()
    service.resolver = Resolver()
    service.settings = _settings(gateway_anthropic_probe_model="claude-haiku-5-5")
    service.actor_user_id = "admin"
    service.repository = Repository()
    service.audit = Audit()
    result = await service.probe(provider.id, ConnectionProbeInput(expected_version=1, mode="chat"))

    assert result.status == expected_status
    assert received[0][1]["api_key"] == "key-a"
    assert received[0][1]["model_profiles"] == {"claude-haiku-5-5": model}
    assert received[1][1] == "Reply OK."
    assert received[1][2]["model_id"] == "claude-haiku-5-5"
    assert received[1][2]["options"].hard_completion_token_limit == 64
    assert received[1][2]["options"].low_reasoning is True


def test_anthropic_chat_probe_is_target_only_and_uses_configured_default_model():
    service = object.__new__(ProviderConnectionProbeService)
    service.settings = _settings(gateway_anthropic_probe_model="claude-haiku-5-5")
    payload = ConnectionProbeInput(expected_version=1, mode="chat")

    service._validate_mode("anthropic", "target", payload)
    assert service._default_chat_model("anthropic") == "claude-haiku-5-5"
    with pytest.raises(ProviderProbeInvalidError, match="target"):
        service._validate_mode("anthropic", "internal", payload)
