from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.models.gateway import Provider
from tests.service_app import app


@pytest_asyncio.fixture
async def probe_client(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "probe.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(
            id="user-1",
            email="user@example.com",
            roles=("admin",),
            permissions=frozenset({"models.read", "models.manage"}),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, factory
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_model_create_and_offline_probe_never_expose_provider_secret(probe_client, monkeypatch):
    from src.backend.services import model_catalog_service
    from src.shared.config import Settings

    monkeypatch.setattr(
        model_catalog_service,
        "get_settings",
        lambda: Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", gateway_execution_mode="offline"),
    )
    client, factory = probe_client
    async with factory() as session:
        provider = Provider(
            id=str(uuid4()),
            name="offline",
            display_name="Offline",
            kind="offline",
            secret_env_var="OFFLINE_SECRET",
        )
        session.add(provider)
        await session.commit()
        provider_id = provider.id

    created = await client.post(
        "/api/v1/gateway/models",
        json={
            "provider_id": provider_id,
            "name": "local-deterministic",
            "display_name": "Local deterministic",
            "capabilities": ["chat"],
        },
    )
    assert created.status_code == 201
    assert created.json()["status"] == "inactive"
    assert "secret_env_var" not in created.json()
    model_id = created.json()["id"]

    rejected_activation = await client.patch(f"/api/v1/gateway/models/{model_id}", json={"status": "active"})
    assert rejected_activation.status_code == 422

    probe = await client.post(f"/api/v1/gateway/models/{model_id}/probe")

    assert probe.status_code == 200
    assert probe.json()["status"] == "offline"
    assert probe.json()["latency_ms"] >= 0
    assert probe.json()["checked_at"]
    assert probe.json()["credential_version"] is None
    assert "secret" not in probe.text.lower()

    activated = await client.patch(f"/api/v1/gateway/models/{model_id}", json={"status": "active"})
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "active"


@pytest.mark.asyncio
async def test_capability_edit_resets_health_and_requires_probe_again(probe_client, monkeypatch):
    from src.backend.services import model_catalog_service
    from src.shared.config import Settings
    from src.shared.models.gateway import Model

    monkeypatch.setattr(
        model_catalog_service,
        "get_settings",
        lambda: Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", gateway_execution_mode="offline"),
    )
    client, factory = probe_client
    provider_response = await client.post(
        "/api/v1/gateway/providers", json={"name": "capability-offline", "display_name": "Offline", "kind": "offline"}
    )
    model_response = await client.post(
        "/api/v1/gateway/models",
        json={
            "provider_id": provider_response.json()["id"],
            "name": "capability-model",
            "display_name": "Capability model",
        },
    )
    model_id = model_response.json()["id"]
    probe = await client.post(f"/api/v1/gateway/models/{model_id}/probe")
    assert probe.status_code == 200
    assert (await client.patch(f"/api/v1/gateway/models/{model_id}", json={"status": "active"})).status_code == 200

    edited = await client.patch(f"/api/v1/gateway/models/{model_id}", json={"capabilities": ["embedding"]})

    assert edited.status_code == 200
    assert edited.json()["status"] == "inactive"
    assert edited.json()["health_status"] == "unknown"
    assert edited.json()["last_probe_credential_version"] is None
    async with factory() as session:
        model = await session.get(Model, model_id)
        assert model.health_status == "unknown"


@pytest.mark.asyncio
async def test_probe_transport_failure_is_safe_and_preserves_model_health(probe_client):
    import httpx

    from src.backend.clients.ai_backend_client import AIBackendClient, get_ai_backend_client
    from src.shared.config import Settings
    from src.shared.models.gateway import Model

    client, factory = probe_client
    created_provider = await client.post(
        "/api/v1/gateway/providers", json={"name": "probe-offline", "display_name": "Offline", "kind": "offline"}
    )
    assert created_provider.status_code == 201
    created_model = await client.post(
        "/api/v1/gateway/models",
        json={"provider_id": created_provider.json()["id"], "name": "test-model", "display_name": "Test"},
    )
    assert created_model.status_code == 201
    model_id = created_model.json()["id"]
    async with factory() as session:
        model = await session.get(Model, model_id)
        model.health_status = "healthy"
        await session.commit()

    def unavailable(request):
        raise httpx.ConnectError("private-host service-secret", request=request)

    app.dependency_overrides[get_ai_backend_client] = lambda: AIBackendClient(
        Settings(_env_file=None, ai_service_token="service-secret"), transport=httpx.MockTransport(unavailable)
    )
    response = await client.post(f"/api/v1/gateway/models/{model_id}/probe")
    assert response.status_code == 503
    assert response.json() == {"detail": "AI service is unavailable."}
    async with factory() as session:
        model = await session.get(Model, model_id)
        assert model.health_status == "healthy"
        assert model.last_probe_at is None


@pytest.mark.asyncio
async def test_live_activation_requires_probe_for_current_credential_version(probe_client, monkeypatch):
    from datetime import UTC, datetime

    from src.backend.services import model_catalog_service
    from src.shared.config import Settings
    from src.shared.models.gateway import Model, Provider
    from src.shared.models.provider_credentials import ProviderCredential

    client, factory = probe_client
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        gateway_execution_mode="live",
        gateway_openai_enabled=True,
    )
    monkeypatch.setattr(model_catalog_service, "get_settings", lambda: settings)
    async with factory() as session:
        provider = Provider(
            id=str(uuid4()),
            name="live-openai",
            display_name="Live OpenAI",
            kind="openai",
            purpose="target",
            status="active",
        )
        model = Model(
            id=str(uuid4()),
            provider_id=provider.id,
            name="gpt-probe-version",
            display_name="Probe version",
            status="inactive",
            health_status="healthy",
            last_probe_at=datetime.now(UTC),
            last_probe_credential_version=1,
            capabilities_json=["chat"],
        )
        credential = ProviderCredential(
            provider_id=provider.id, encrypted_api_key="dummy-ciphertext", version=2, status="active"
        )
        session.add_all([provider, model, credential])
        await session.commit()
        model_id = model.id

    stale = await client.patch(f"/api/v1/gateway/models/{model_id}", json={"status": "active"})
    assert stale.status_code == 422

    async with factory() as session:
        model = await session.get(Model, model_id)
        model.last_probe_credential_version = 2
        await session.commit()
    current = await client.patch(f"/api/v1/gateway/models/{model_id}", json={"status": "active"})
    assert current.status_code == 200, current.text
    assert current.json()["status"] == "active"


@pytest.mark.asyncio
async def test_live_model_probe_uses_selected_connection_and_its_version(monkeypatch):

    from src.backend_ai.services import model_probe_execution_service as module
    from src.backend_ai.services.model_probe_execution_service import ModelProbeExecutionService
    from src.shared.config import Settings
    from src.shared.models.gateway import Model, Provider

    model = Model(
        id="selected-model",
        provider_id="selected-provider",
        name="gpt-5.6-sol",
        display_name="GPT-5.6 Sol",
        status="inactive",
        capabilities_json=["chat"],
    )
    provider = Provider(
        id="selected-provider",
        name="openai-account-b",
        display_name="OpenAI account B",
        kind="openai",
        purpose="target",
        status="active",
    )
    client_calls = []

    class Session:
        async def get(self, entity, entity_id):
            return model if entity is Model else provider if entity_id == provider.id else None

        def in_transaction(self):
            return False

    class CredentialResolver:
        def __init__(self, *_args):
            pass

    class Adapter:
        async def generate(self, prompt, **kwargs):
            client_calls.append((prompt, kwargs["model_id"], kwargs["options"]))

    class Builder:
        def __init__(self, _resolver, _settings):
            self.credential_versions = {}

        async def build_for_provider(self, selected_provider):
            assert selected_provider.id == "selected-provider"
            self.credential_versions[selected_provider.id] = 9
            return Adapter()

    monkeypatch.setattr(module, "ProviderCredentialResolver", CredentialResolver)
    monkeypatch.setattr(module, "ensure_credential_crypto", lambda _settings: object())
    monkeypatch.setattr(module, "ProviderClientBuilder", Builder)
    result = await ModelProbeExecutionService(
        Session(),
        Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///:memory:",
            gateway_execution_mode="live",
            gateway_openai_enabled=True,
        ),
    ).probe(model.id)

    assert result.status == "healthy"
    assert result.credential_version == 9
    assert client_calls[0][:2] == ("Reply OK.", "gpt-5.6-sol")
    assert client_calls[0][2].max_output_tokens == 8
    assert client_calls[0][2].hard_completion_token_limit is None


@pytest.mark.asyncio
async def test_embedding_only_model_probe_uses_embedding_adapter(monkeypatch):
    from types import SimpleNamespace

    from src.backend_ai.services import model_probe_execution_service as module
    from src.backend_ai.services.model_probe_execution_service import ModelProbeExecutionService
    from src.shared.config import Settings
    from src.shared.models.gateway import Model, Provider

    model = Model(
        id="embedding-model",
        provider_id="embedding-provider",
        name="text-embedding-3-small",
        display_name="Embedding model",
        status="inactive",
        capabilities_json=["embedding"],
    )
    provider = Provider(
        id="embedding-provider",
        name="openai-embedding",
        display_name="OpenAI embedding",
        kind="openai",
        purpose="target",
        status="active",
    )
    observed = {}

    class Session:
        async def get(self, entity, _entity_id):
            return model if entity is Model else provider

        def in_transaction(self):
            return False

    class CredentialResolver:
        def __init__(self, *_args):
            pass

        async def resolve_provider(self, provider_id):
            assert provider_id == provider.id
            return SimpleNamespace(api_key="selected-db-key", version=4)

    class EmbeddingAdapter:
        def __init__(self, *, api_key, base_url):
            observed["api_key"] = api_key

        async def probe(self, model_name, *, timeout_seconds):
            observed["probe"] = (model_name, timeout_seconds)

    monkeypatch.setattr(module, "ProviderCredentialResolver", CredentialResolver)
    monkeypatch.setattr(module, "ensure_credential_crypto", lambda _settings: object())
    monkeypatch.setattr(module, "OpenAIEmbeddingProvider", EmbeddingAdapter)
    result = await ModelProbeExecutionService(
        Session(),
        Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///:memory:",
            gateway_execution_mode="live",
            gateway_openai_enabled=True,
        ),
    ).probe(model.id)

    assert result.status == "healthy"
    assert result.credential_version == 4
    assert observed["api_key"] == "selected-db-key"
    assert observed["probe"][0] == "text-embedding-3-small"


@pytest.mark.asyncio
@pytest.mark.parametrize("text, expected_status", [("OK", "healthy"), ("", "unhealthy")])
async def test_anthropic_model_probe_uses_catalog_profile_low_effort_and_rejects_empty_output(monkeypatch, text,
                                                                                             expected_status):
    from types import SimpleNamespace

    from src.backend_ai.services import model_probe_execution_service as module
    from src.backend_ai.services.model_probe_execution_service import ModelProbeExecutionService
    from src.shared.config import Settings
    from src.shared.models.gateway import Model, Provider

    model = Model(id="anthropic-model", provider_id="anthropic-provider", name="claude-sonnet-5-5",
        display_name="Claude Sonnet 5.5", status="inactive", max_output_tokens=128_000,
        feature_metadata_json={"constraints": {"thinking_mode": "adaptive", "default_effort": "high",
            "forced_tool_choice": "conditional"}}, capabilities_json=["chat"])
    provider = Provider(id=model.provider_id, name="anthropic", display_name="Anthropic", kind="anthropic",
        purpose="target", status="active", base_url="https://api.anthropic.com")
    observed = {}

    class Session:
        async def get(self, entity, _entity_id):
            return model if entity is Model else provider

        def in_transaction(self):
            return False

    class CredentialResolver:
        def __init__(self, *_args):
            pass

    class Adapter:
        async def generate(self, prompt, **kwargs):
            observed["call"] = (prompt, kwargs)
            return SimpleNamespace(text=text, metadata={})

    class Builder:
        def __init__(self, _resolver, _settings):
            self.credential_versions = {provider.id: 9}

        async def build_for_provider(self, selected_provider, *, model_profile=None):
            assert selected_provider.id == provider.id
            observed["profile"] = model_profile
            return Adapter()

    monkeypatch.setattr(module, "ProviderCredentialResolver", CredentialResolver)
    monkeypatch.setattr(module, "ensure_credential_crypto", lambda _settings: object())
    monkeypatch.setattr(module, "ProviderClientBuilder", Builder)
    result = await ModelProbeExecutionService(Session(), Settings(_env_file=None,
        database_url="sqlite+aiosqlite:///:memory:", gateway_execution_mode="live",
        gateway_anthropic_enabled=True)).probe(model.id)

    assert result.status == expected_status
    assert result.credential_version == 9
    assert observed["profile"] is model
    assert observed["call"][0] == "Reply OK."
    options = observed["call"][1]["options"]
    assert options.max_output_tokens == options.hard_completion_token_limit == 64
    assert options.low_reasoning is True


@pytest.mark.asyncio
async def test_model_probe_latency_updates_probe_measurement_without_changing_catalog_baseline():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from src.backend.services.model_probe_service import ModelProbeService
    from src.shared.dtos.model_catalog import ModelProbeRead
    from src.shared.models.gateway import Model

    model = Model(id="anthropic-model", provider_id="anthropic-provider", name="claude-haiku-5-5",
        display_name="Claude Haiku 5.5", target_latency_ms=None, last_probe_latency_ms=900.0)

    class Catalog:
        async def get(self, model_id):
            assert model_id == model.id
            return model, SimpleNamespace(kind="anthropic")

    class AIClient:
        async def post(self, _path, _payload):
            return ModelProbeRead(model_id=model.id, status="healthy", checked_at=datetime.now(UTC),
                latency_ms=125.5, credential_version=2).model_dump(mode="json")

    class Session:
        async def commit(self):
            return None

    service = ModelProbeService(Session(), AIClient())
    service.catalog = Catalog()

    result = await service.probe(model.id)

    assert result.latency_ms == 125.5
    assert model.target_latency_ms is None
    assert model.last_probe_latency_ms == 125.5
