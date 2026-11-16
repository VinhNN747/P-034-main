"""Offline pipelines: built only from self-hosted models, kept apart from applications, keys with the off_ prefix."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.main import app
from src.backend.routers import customer_endpoints as endpoints_router
from src.backend.services import customer_endpoint_service, offline_pipeline_service
from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.models.core import Application
from src.shared.models.gateway import Model, ModelPool, Provider
from src.shared.services.credential_crypto import CredentialCrypto

PUBLIC = "https://8.8.8.8/v1"  # an IP literal: resolves without DNS, so the test stays offline
BASE = "/api/v1/gateway/offline-pipelines"
PERMISSIONS = frozenset({
    "applications.read_own", "applications.manage_own", "model_pools.read_own", "model_pools.manage_own",
    "models.read_available",
})


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", **overrides)


@pytest_asyncio.fixture
async def ctx(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'offline.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    settings = _settings(credential_encryption_key=Fernet.generate_key().decode())
    crypto = CredentialCrypto.from_settings(settings)
    state = SimpleNamespace(user="user-1", permissions=PERMISSIONS, factory=factory, settings=settings)
    monkeypatch.setattr(customer_endpoint_service, "get_settings", lambda: state.settings)
    monkeypatch.setattr(offline_pipeline_service, "get_settings", lambda: state.settings)

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(id=state.user, email=f"{state.user}@example.com", roles=("user",), permissions=state.permissions)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[endpoints_router._crypto] = lambda: crypto
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        state.client = client
        yield state
    app.dependency_overrides.clear()
    await engine.dispose()


async def _ladder(ctx, *, active=True) -> dict[str, str]:
    """Three models on one of the user's endpoints, one per tier, probed (active) unless told otherwise."""
    created = await ctx.client.post("/api/v1/gateway/my-endpoints", json={"display_name": "GPU", "base_url": PUBLIC})
    assert created.status_code == 201, created.text
    endpoint_id = created.json()["id"]
    ids = {}
    for tier, name in (("simple", "qwen:0.5b"), ("mid", "qwen:1.5b"), ("hard", "qwen:3b")):
        added = await ctx.client.post(f"/api/v1/gateway/my-endpoints/{endpoint_id}/models", json={"name": name, "quality_tier": tier})
        assert added.status_code == 201, added.text
        ids[tier] = next(m["id"] for m in added.json()["models"] if m["name"] == name)
    if active:
        async with ctx.factory() as session:
            for model_id in ids.values():
                model = await session.get(Model, model_id)
                model.status, model.health_status = "active", "healthy"
            await session.commit()
    return ids


def _payload(ladder, **overrides):
    return {"slug": "chatbot-noi-bo", "name": "Chatbot nội bộ", "models": ladder, **overrides}


async def _external_model(ctx, tier="mid") -> str:
    async with ctx.factory() as session:
        provider = Provider(name="openai-main", display_name="OpenAI", kind="openai", purpose="target", status="active")
        session.add(provider)
        await session.flush()
        model = Model(provider_id=provider.id, name="gpt-4o-mini", display_name="GPT-4o mini", status="active",
                      quality_tier=tier, capabilities_json=["chat"])
        session.add(model)
        await session.commit()
        return model.id


@pytest.mark.asyncio
async def test_create_builds_an_offline_pipeline_kept_apart_from_applications(ctx):
    ladder = await _ladder(ctx)
    created = await ctx.client.post(BASE, json=_payload(ladder))
    assert created.status_code == 201, created.text
    body = created.json()
    assert [m["tier"] for m in body["models"]] == ["simple", "mid", "hard"]
    assert [m["model_id"] for m in body["models"]] == [ladder["simple"], ladder["mid"], ladder["hard"]]
    assert body["models"][0]["endpoint_name"] == "GPU"
    assert (body["classifier_mid_difficulty"], body["classifier_large_difficulty"], body["thresholds_default"]) == (0.23, 0.61, True)
    assert body["status"] == "active" and body["max_output_tokens"] == 800 and body["timeout_seconds"] == 60

    assert [item["id"] for item in (await ctx.client.get(BASE)).json()] == [body["id"]]
    assert (await ctx.client.get(f"{BASE}/{body['id']}")).json()["name"] == "Chatbot nội bộ"
    # Not an application, and its pool is not a reusable model pool.
    assert (await ctx.client.get("/api/v1/applications")).json()["total"] == 0
    pools = (await ctx.client.get("/api/v1/gateway/model-pools")).json()
    assert all(not pool["name"].startswith("offline:") for pool in pools)

    async with ctx.factory() as session:
        application = await session.get(Application, body["id"])
        assert application.pipeline == "offline"
    config = (await ctx.client.get(f"/api/v1/applications/{body['id']}/gateway-config")).json()
    assert (config["default_understander"], config["default_decomposer"], config["default_aggregator"]) == (
        "classifier", "none", "deterministic")


@pytest.mark.asyncio
async def test_keys_of_an_offline_pipeline_start_with_off(ctx):
    pipeline = (await ctx.client.post(BASE, json=_payload(await _ladder(ctx)))).json()
    online = (await ctx.client.post("/api/v1/applications", json={"slug": "shop", "name": "Shop"})).json()

    offline_key = (await ctx.client.post(f"/api/v1/applications/{pipeline['id']}/api-keys", json={"name": "k"})).json()
    online_key = (await ctx.client.post(f"/api/v1/applications/{online['id']}/api-keys", json={"name": "k"})).json()
    assert offline_key["raw_key"].startswith("off_") and offline_key["key_prefix"].startswith("off_")
    assert online_key["raw_key"].startswith("ak_")


@pytest.mark.asyncio
async def test_only_self_hosted_active_models_of_the_right_tier_are_accepted(ctx):
    ladder = await _ladder(ctx)
    external = await _external_model(ctx)
    cases = [
        ({**ladder, "mid": external}, "not on a self-hosted endpoint"),
        ({**ladder, "mid": ladder["hard"], "hard": ladder["mid"]}, "is registered as"),
        ({**ladder, "hard": ladder["simple"]}, "three different models"),
        ({**ladder, "mid": "missing-model"}, "model not found"),
    ]
    for models, message in cases:
        refused = await ctx.client.post(BASE, json=_payload(models))
        assert refused.status_code == 422, (message, refused.text)
        assert message in refused.json()["detail"]
    assert (await ctx.client.get(BASE)).json() == []
    async with ctx.factory() as session:
        assert list(await session.scalars(select(ModelPool))) == []  # nothing left behind


@pytest.mark.asyncio
async def test_a_model_must_be_probed_first(ctx):
    refused = await ctx.client.post(BASE, json=_payload(await _ladder(ctx, active=False)))
    assert refused.status_code == 422 and "probe" in refused.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("thresholds", [{"classifier_mid_difficulty": 0.3}, {"classifier_mid_difficulty": 0.6, "classifier_large_difficulty": 0.4}])
async def test_thresholds_come_in_ordered_pairs(ctx, thresholds):
    refused = await ctx.client.post(BASE, json=_payload(await _ladder(ctx), **thresholds))
    assert refused.status_code == 422


@pytest.mark.asyncio
async def test_the_generic_apis_cannot_change_or_delete_an_offline_pipeline(ctx):
    pipeline = (await ctx.client.post(BASE, json=_payload(await _ladder(ctx)))).json()
    external = await _external_model(ctx)

    config = (await ctx.client.get(f"/api/v1/applications/{pipeline['id']}/gateway-config")).json()
    put = await ctx.client.put(f"/api/v1/applications/{pipeline['id']}/gateway-config", json={**config, "model_ids": [external]})
    assert put.status_code == 409
    assert (await ctx.client.delete(f"/api/v1/applications/{pipeline['id']}")).status_code == 409

    pool_id = config["model_pool_id"]
    assert (await ctx.client.delete(f"/api/v1/gateway/model-pools/{pool_id}")).status_code == 409
    update = await ctx.client.put(f"/api/v1/gateway/model-pools/{pool_id}", json={
        "name": "x", "purpose": "general", "members": [{"model_id": external}]})
    assert update.status_code in (409, 422)

    # And an online application cannot borrow the offline pipeline's pool.
    online = (await ctx.client.post("/api/v1/applications", json={"slug": "shop", "name": "Shop"})).json()
    borrow = await ctx.client.put(f"/api/v1/applications/{online['id']}/gateway-config", json={"model_pool_id": pool_id})
    assert borrow.status_code == 422

    # Deleting an endpoint under the pipeline names the pipeline, not its internal pool.
    endpoint_id = pipeline["models"][0]["endpoint_id"]
    in_use = await ctx.client.delete(f"/api/v1/gateway/my-endpoints/{endpoint_id}")
    assert in_use.status_code == 409
    assert f"offline pipelines {pipeline['name']}" in in_use.json()["detail"]
    assert "offline:" not in in_use.json()["detail"]


@pytest.mark.asyncio
async def test_update_changes_thresholds_status_and_models_then_delete_cleans_up(ctx):
    ladder = await _ladder(ctx)
    pipeline = (await ctx.client.post(BASE, json=_payload(ladder))).json()

    tuned = await ctx.client.patch(f"{BASE}/{pipeline['id']}", json={
        "classifier_mid_difficulty": 0.25, "classifier_large_difficulty": 0.55, "status": "paused", "max_output_tokens": 300})
    assert tuned.status_code == 200, tuned.text
    body = tuned.json()
    assert (body["classifier_mid_difficulty"], body["classifier_large_difficulty"], body["thresholds_default"]) == (0.25, 0.55, False)
    assert body["status"] == "paused" and body["max_output_tokens"] == 300

    back = (await ctx.client.patch(f"{BASE}/{pipeline['id']}", json={
        "classifier_mid_difficulty": None, "classifier_large_difficulty": None})).json()
    assert back["thresholds_default"] is True and back["classifier_mid_difficulty"] == 0.23
    assert back["max_output_tokens"] == 300  # untouched fields stay

    other = await _ladder(ctx)
    swapped = (await ctx.client.patch(f"{BASE}/{pipeline['id']}", json={"models": other})).json()
    assert [m["model_id"] for m in swapped["models"]] == [other["simple"], other["mid"], other["hard"]]
    assert (await ctx.client.patch(f"{BASE}/{pipeline['id']}", json={"models": {**other, "mid": await _external_model(ctx)}})).status_code == 422

    assert (await ctx.client.delete(f"{BASE}/{pipeline['id']}")).status_code == 204
    assert (await ctx.client.get(f"{BASE}/{pipeline['id']}")).status_code == 404
    async with ctx.factory() as session:
        assert list(await session.scalars(select(ModelPool))) == []


@pytest.mark.asyncio
async def test_another_users_pipeline_and_endpoint_stay_invisible(ctx):
    ladder = await _ladder(ctx)
    pipeline = (await ctx.client.post(BASE, json=_payload(ladder))).json()
    ctx.user = "user-2"
    assert (await ctx.client.get(BASE)).json() == []
    assert (await ctx.client.get(f"{BASE}/{pipeline['id']}")).status_code == 404
    stolen = await ctx.client.post(BASE, json=_payload(ladder, slug="steal", name="Steal"))
    assert stolen.status_code == 422 and "model not found" in stolen.json()["detail"]


@pytest.mark.asyncio
async def test_managing_needs_the_pool_permission_too(ctx):
    ladder = await _ladder(ctx)
    ctx.permissions = PERMISSIONS - {"model_pools.manage_own"}
    assert (await ctx.client.post(BASE, json=_payload(ladder))).status_code == 403
    assert (await ctx.client.get(BASE)).status_code == 200
