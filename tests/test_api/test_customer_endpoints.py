"""Bring-your-own endpoints: SSRF rules, per-user isolation, credentials and the runtime guard."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.main import app
from src.backend.routers import customer_endpoints as endpoints_router
from src.backend.services import customer_endpoint_service
from src.backend_ai.gateway.providers.base import GenerationOptions, PermanentProviderError
from src.backend_ai.gateway.providers.local import LocalProvider
from src.backend_ai.services.provider_client_builder import ProviderClientBuilder
from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.dtos.model_catalog import ModelProbeRead
from src.shared.models.gateway import Model, Provider
from src.shared.models.provider_credentials import ProviderCredential
from src.shared.repositories.routing_catalog import routing_candidates
from src.shared.services.credential_crypto import CredentialCrypto
from src.shared.services.local_endpoints import LocalEndpointError, normalize_local_base_url

PUBLIC = "https://8.8.8.8/v1"  # an IP literal: resolves without DNS, so the test stays offline


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", **overrides)


@pytest_asyncio.fixture
async def ctx(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'byo.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    settings = _settings(credential_encryption_key=Fernet.generate_key().decode())
    crypto = CredentialCrypto.from_settings(settings)
    state = SimpleNamespace(user="user-1", factory=factory, crypto=crypto, settings=settings)
    monkeypatch.setattr(customer_endpoint_service, "get_settings", lambda: state.settings)

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(
            id=state.user, email=f"{state.user}@example.com", roles=("owner",),
            permissions=frozenset({"model_pools.read_own", "model_pools.manage_own", "models.read_available"}),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[endpoints_router._crypto] = lambda: crypto
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        state.client = client
        yield state
    app.dependency_overrides.clear()
    await engine.dispose()


async def _endpoint_with_active_model(ctx, *, name="qwen2.5:1.5b", tier="simple"):
    created = await ctx.client.post("/api/v1/gateway/my-endpoints", json={"display_name": "GPU", "base_url": PUBLIC})
    assert created.status_code == 201, created.text
    endpoint_id = created.json()["id"]
    added = await ctx.client.post(
        f"/api/v1/gateway/my-endpoints/{endpoint_id}/models", json={"name": name, "quality_tier": tier}
    )
    assert added.status_code == 201, added.text
    model_id = added.json()["models"][0]["id"]
    async with ctx.factory() as session:
        model = await session.get(Model, model_id)
        model.status, model.health_status = "active", "healthy"
        await session.commit()
    return endpoint_id, model_id


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8/v1",  # not HTTPS
        "https://localhost:11434",
        "https://127.0.0.1:8000/v1",
        "https://10.0.0.5/v1",
        "https://192.168.1.20/v1",
        "https://169.254.169.254/latest",  # cloud metadata
        "https://[::1]/v1",
        "https://[::ffff:127.0.0.1]/v1",
        "https://gpu.internal/v1",
    ],
)
def test_hosted_mode_refuses_private_or_plain_http_endpoints(url):
    with pytest.raises(LocalEndpointError):
        normalize_local_base_url(url, allow_private=False)


def test_self_hosted_mode_keeps_localhost():
    assert normalize_local_base_url("http://localhost:11434", allow_private=True) == "http://localhost:11434/v1"


@pytest.mark.asyncio
async def test_create_rejects_a_private_url_by_default_and_allows_it_when_configured(ctx):
    payload = {"display_name": "Home GPU", "base_url": "http://localhost:11434"}

    refused = await ctx.client.post("/api/v1/gateway/my-endpoints", json=payload)
    assert refused.status_code == 422

    ctx.settings = _settings(gateway_local_endpoints_allow_private=True)
    accepted = await ctx.client.post("/api/v1/gateway/my-endpoints", json=payload)
    assert accepted.status_code == 201
    assert accepted.json()["base_url"] == "http://localhost:11434/v1"


@pytest.mark.asyncio
async def test_api_key_is_stored_encrypted_and_never_returned(ctx):
    created = await ctx.client.post(
        "/api/v1/gateway/my-endpoints",
        json={"display_name": "GPU", "base_url": PUBLIC, "api_key": "sk-local-secret-1234"},
    )

    body = created.json()
    assert created.status_code == 201
    assert (body["has_api_key"], body["key_last4"]) == (True, "1234")
    assert "sk-local-secret" not in created.text
    async with ctx.factory() as session:
        provider = await session.get(Provider, body["id"])
        credential = await session.scalar(select(ProviderCredential).where(ProviderCredential.provider_id == body["id"]))
    assert (provider.owner_user_id, provider.kind, provider.name.startswith("byo-")) == ("user-1", "local", True)
    assert "sk-local-secret" not in credential.encrypted_api_key


@pytest.mark.asyncio
async def test_another_user_cannot_see_use_or_delete_an_endpoint(ctx):
    endpoint_id, model_id = await _endpoint_with_active_model(ctx)

    ctx.user = "user-2"
    assert (await ctx.client.get("/api/v1/gateway/my-endpoints")).json() == []
    assert (await ctx.client.get(f"/api/v1/gateway/my-endpoints/{endpoint_id}")).status_code == 404
    assert (await ctx.client.delete(f"/api/v1/gateway/my-endpoints/{endpoint_id}")).status_code == 404
    available = await ctx.client.get("/api/v1/gateway/available-models")
    assert model_id not in {item["id"] for item in available.json()["items"]}
    suggestion = await ctx.client.post(
        "/api/v1/gateway/model-pools/fallback-suggestions", json={"model_ids": [model_id]}
    )
    assert suggestion.status_code == 422

    ctx.user = "user-1"
    available = await ctx.client.get("/api/v1/gateway/available-models")
    assert model_id in {item["id"] for item in available.json()["items"]}


@pytest.mark.asyncio
async def test_only_the_owner_can_put_an_endpoint_model_in_a_pool(ctx):
    _, small = await _endpoint_with_active_model(ctx, name="s", tier="simple")
    _, mid = await _endpoint_with_active_model(ctx, name="m", tier="mid")
    _, hard = await _endpoint_with_active_model(ctx, name="h", tier="hard")
    pool = {"name": "Local", "purpose": "local", "status": "active",
            "members": [{"model_id": small}, {"model_id": mid}, {"model_id": hard}]}

    ctx.user = "user-2"
    foreign = await ctx.client.post("/api/v1/gateway/model-pools", json=pool)
    assert foreign.status_code == 422
    assert "Active model not found" in foreign.text

    ctx.user = "user-1"
    own = await ctx.client.post("/api/v1/gateway/model-pools", json=pool)
    assert own.status_code == 201, own.text
    async with ctx.factory() as session:
        rows = await routing_candidates(session, own.json()["id"])
        shared = await routing_candidates(session, None)
    assert {model.id for _, model, _ in rows} == {small, mid, hard}
    # The shared catalog (an application without a pool) never contains a customer endpoint.
    assert not {model.id for _, model, _ in shared} & {small, mid, hard}

    in_use = await ctx.client.delete(f"/api/v1/gateway/my-endpoints/{(await ctx.client.get('/api/v1/gateway/my-endpoints')).json()[0]['id']}")
    assert in_use.status_code == 409
    assert "model pools Local" in in_use.json()["detail"]


@pytest.mark.asyncio
async def test_probe_activates_a_healthy_model_and_deactivates_a_failing_one(ctx):
    created = await ctx.client.post("/api/v1/gateway/my-endpoints", json={"display_name": "GPU", "base_url": PUBLIC})
    endpoint_id = created.json()["id"]
    model_id = (
        await ctx.client.post(f"/api/v1/gateway/my-endpoints/{endpoint_id}/models",
                              json={"name": "qwen", "quality_tier": "mid", "context_window": 4096})
    ).json()["models"][0]["id"]

    class FakeProbe:
        def __init__(self, session, status):
            self.session, self.status = session, status

        async def probe(self, probed_id):
            model = await self.session.get(Model, probed_id)
            model.health_status, model.last_probe_at = self.status, datetime.now(UTC)
            await self.session.commit()
            return ModelProbeRead(model_id=probed_id, status=self.status, checked_at=datetime.now(UTC),
                                  latency_ms=12.0, credential_version=None, failure_reason=None)

    async with ctx.factory() as session:
        service = customer_endpoint_service.CustomerEndpointService(session, ctx.crypto)
        healthy = await service.probe_model("user-1", endpoint_id, model_id, probe_service=FakeProbe(session, "healthy"))
        assert healthy.models[0].status == "active"
        failing = await service.probe_model("user-1", endpoint_id, model_id, probe_service=FakeProbe(session, "unhealthy"))
        assert failing.models[0].status == "inactive"
        with pytest.raises(customer_endpoint_service.EndpointNotFoundError):
            await service.probe_model("user-2", endpoint_id, model_id, probe_service=FakeProbe(session, "healthy"))
        model = await session.get(Model, model_id)
    assert (model.input_price_per_million, model.output_price_per_million) == (0.0, 0.0)


@pytest.mark.asyncio
async def test_builder_holds_customer_endpoints_to_the_public_address_rule():
    customer = SimpleNamespace(id="p1", name="byo-1", kind="local", purpose="target", status="active",
                               base_url="http://127.0.0.1:9/v1", owner_user_id="user-1")
    system = SimpleNamespace(**{**vars(customer), "owner_user_id": None})

    hosted = ProviderClientBuilder(None, _settings())
    assert (await hosted.build_for_provider(customer))._allow_private is False
    assert (await hosted.build_for_provider(system))._allow_private is True
    self_hosted = ProviderClientBuilder(None, _settings(gateway_local_endpoints_allow_private=True))
    assert (await self_hosted.build_for_provider(customer))._allow_private is True


@pytest.mark.asyncio
async def test_runtime_guard_refuses_a_private_address_before_any_request():
    provider = LocalProvider(base_url="https://127.0.0.1:9/v1", allow_private=False)

    with pytest.raises(PermanentProviderError, match="public internet"):
        await provider.generate("hi", task_id="t", model_id="m", timeout_seconds=2,
                                options=GenerationOptions(max_output_tokens=4))
    with pytest.raises(PermanentProviderError):
        [event async for event in provider.stream("hi", task_id="t", model_id="m", timeout_seconds=2)]


@pytest.mark.asyncio
async def test_endpoint_models_can_serve_coding_and_reasoning_requests(ctx):
    created = await ctx.client.post("/api/v1/gateway/my-endpoints", json={"display_name": "GPU", "base_url": PUBLIC})
    endpoint_id = created.json()["id"]

    plain = await ctx.client.post(f"/api/v1/gateway/my-endpoints/{endpoint_id}/models",
                                  json={"name": "qwen", "quality_tier": "mid"})
    declared = await ctx.client.post(f"/api/v1/gateway/my-endpoints/{endpoint_id}/models",
                                     json={"name": "qwen-json", "quality_tier": "hard",
                                           "capabilities": ["structured_output"]})

    by_name = {model["name"]: model["capabilities"] for model in declared.json()["models"]}
    assert plain.status_code == 201
    # "text" satisfies the code_generation / reasoning requirement a classifier adds.
    assert by_name["qwen"] == ["chat", "text"]
    assert by_name["qwen-json"] == ["chat", "text", "structured_output"]


@pytest.mark.asyncio
async def test_discover_lists_the_models_the_server_serves(ctx, monkeypatch):
    seen = {}

    def server(request):
        seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "qwen2.5:7b"}, {"id": "qwen2.5:0.5b"}, {"bad": 1}]})

    monkeypatch.setattr(customer_endpoint_service.CustomerEndpointService, "discovery_transport", httpx.MockTransport(server))
    created = await ctx.client.post(
        "/api/v1/gateway/my-endpoints", json={"display_name": "GPU", "base_url": PUBLIC, "api_key": "sk-test"}
    )
    endpoint_id = created.json()["id"]

    found = await ctx.client.get(f"/api/v1/gateway/my-endpoints/{endpoint_id}/discover")

    assert found.status_code == 200, found.text
    assert found.json() == ["qwen2.5:0.5b", "qwen2.5:7b"]
    assert seen == {"url": "https://8.8.8.8/v1/models", "auth": "Bearer sk-test"}
    ctx.user = "user-2"
    assert (await ctx.client.get(f"/api/v1/gateway/my-endpoints/{endpoint_id}/discover")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [httpx.Response(401), httpx.Response(302, headers={"location": "http://10.0.0.1/"}), httpx.Response(200, text="hi")],
)
async def test_discover_reports_a_server_that_does_not_list_models(ctx, monkeypatch, response):
    monkeypatch.setattr(
        customer_endpoint_service.CustomerEndpointService, "discovery_transport", httpx.MockTransport(lambda _: response)
    )
    created = await ctx.client.post("/api/v1/gateway/my-endpoints", json={"display_name": "GPU", "base_url": PUBLIC})

    found = await ctx.client.get(f"/api/v1/gateway/my-endpoints/{created.json()['id']}/discover")

    assert found.status_code == 422
    assert "GET /models" in found.json()["detail"]
