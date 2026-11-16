from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from tests.service_app import app


@pytest_asyncio.fixture
async def gateway_client(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog-api.db'}")
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
            roles=("owner",),
            permissions=frozenset({"model_pools.read_own", "model_pools.manage_own", "models.manage"}),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_provider_api_never_accepts_raw_secret(gateway_client):
    response = await gateway_client.post(
        "/api/v1/gateway/providers",
        json={
            "name": "openai",
            "display_name": "OpenAI",
            "kind": "openai",
        },
    )

    assert response.status_code == 201
    assert "api_key" not in response.json()

    unsafe = await gateway_client.post(
        "/api/v1/gateway/providers",
        json={
            "name": "unsafe",
            "display_name": "Unsafe",
            "kind": "custom",
            "secret_env_var": "UNSAFE_KEY",
            "api_key": "raw-secret",
        },
    )
    assert unsafe.status_code == 422


async def _tiered_models(client, provider_name="test-provider", kind="offline", names=None):
    """One model per tier (simple/mid/hard) plus one extra mid model."""
    provider = await client.post(
        "/api/v1/gateway/providers",
        json={"name": provider_name, "display_name": provider_name, "kind": kind},
    )
    assert provider.status_code == 201
    ids = {}
    for name, tier, price in names or (("fast", "simple", 1.0), ("balanced", "mid", 4.0),
                                       ("reasoning", "hard", 20.0), ("balanced-2", "mid", 5.0)):
        response = await client.post(
            "/api/v1/gateway/models",
            json={"provider_id": provider.json()["id"], "name": name, "display_name": name.title(),
                  "quality_tier": tier, "output_price_per_million": price},
        )
        assert response.status_code == 201
        ids[name] = response.json()["id"]
    return ids


@pytest.mark.asyncio
async def test_model_pool_can_be_created_and_replaced_with_unordered_members(gateway_client):
    ids = await _tiered_models(gateway_client)
    ladder = [{"model_id": ids[name]} for name in ("fast", "balanced", "reasoning")]
    created = await gateway_client.post(
        "/api/v1/gateway/model-pools",
        json={"name": "Support", "purpose": "support", "status": "active", "members": ladder},
    )
    assert created.status_code == 201
    assert all(member["role"] == "primary" for member in created.json()["members"])

    updated = await gateway_client.put(
        f"/api/v1/gateway/model-pools/{created.json()['id']}",
        json={
            "name": "Support production", "purpose": "production support", "status": "active",
            "members": [*reversed(ladder), {"model_id": ids["balanced-2"], "role": "fallback"}],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Support production"
    assert {member["model_id"] for member in updated.json()["members"]} == set(ids.values())
    assert {m["model_id"]: m["role"] for m in updated.json()["members"]}[ids["balanced-2"]] == "fallback"
    assert all("priority" not in member and "enabled" not in member for member in updated.json()["members"])


@pytest.mark.asyncio
async def test_pool_api_requires_three_primaries_in_three_tiers(gateway_client):
    ids = await _tiered_models(gateway_client)

    async def create(members):
        return await gateway_client.post(
            "/api/v1/gateway/model-pools", json={"name": "P", "purpose": "p", "members": members},
        )

    too_few = await create([{"model_id": ids["fast"]}])
    assert too_few.status_code == 422 and "exactly 3 primary" in too_few.json()["detail"]
    same_tier = await create([{"model_id": ids[n]} for n in ("fast", "balanced", "balanced-2")])
    assert same_tier.status_code == 422 and "3 different tiers" in same_tier.json()["detail"]
    # A fourth model is fine as a fallback, not as a fourth primary.
    four = await create([{"model_id": ids[n]} for n in ("fast", "balanced", "reasoning", "balanced-2")])
    assert four.status_code == 422


@pytest.mark.asyncio
async def test_pool_read_warns_about_a_backwards_price_ladder_and_legacy_shape(gateway_client):
    ids = await _tiered_models(gateway_client, names=(
        ("fast", "simple", 9.0), ("balanced", "mid", 4.0), ("reasoning", "hard", 20.0), ("balanced-2", "mid", 5.0),
    ))
    created = await gateway_client.post("/api/v1/gateway/model-pools", json={
        "name": "P", "purpose": "p", "members": [{"model_id": ids[n]} for n in ("fast", "balanced", "reasoning")],
    })
    assert created.status_code == 201
    assert any("fast (small) costs more than balanced (mid)" in note for note in created.json()["warnings"])
    assert not any("before the 3-tier rule" in note for note in created.json()["warnings"])


@pytest.mark.asyncio
async def test_fallback_suggestions_follow_same_tier_then_provider_consent(gateway_client):
    ids = await _tiered_models(gateway_client)
    other = await _tiered_models(gateway_client, provider_name="other-provider", kind="local", names=(
        ("other-mid", "mid", 4.5),
    ))
    body = {"model_ids": [ids[n] for n in ("fast", "balanced", "reasoning")]}
    allowed = await gateway_client.post("/api/v1/gateway/model-pools/fallback-suggestions", json=body)
    assert allowed.status_code == 200
    by_tier = {item["tier"]: item for item in allowed.json()}
    # Same tier on another provider first, closest price.
    assert by_tier["mid"]["model_id"] == other["other-mid"]
    assert by_tier["mid"]["reason"] == "same tier, other provider"
    assert by_tier["mid"]["output_price_delta_per_million"] == pytest.approx(0.5)
    # Nothing else callable in the small and large tiers: the next tier up is used.
    assert by_tier["simple"]["model_id"] is None and by_tier["hard"]["model_id"] is None

    same_only = await gateway_client.post(
        "/api/v1/gateway/model-pools/fallback-suggestions", json=body | {"allow_other_providers": False},
    )
    assert {item["tier"]: item for item in same_only.json()}["mid"]["model_id"] == ids["balanced-2"]


@pytest.mark.asyncio
async def test_legacy_admin_claims_cannot_create_or_manage_model_pools(gateway_client):
    async def admin():
        return CurrentUser(id='admin', email=None, roles=('admin',), permissions=frozenset({'model_pools.read','model_pools.manage','models.manage'}))
    app.dependency_overrides[get_current_user] = admin
    response = await gateway_client.post('/api/v1/gateway/model-pools', json={'name':'Admin pool','purpose':'test','members':[{'model_id':'missing'}]})
    assert response.status_code == 403
    assert (await gateway_client.get('/api/v1/gateway/model-pools')).status_code == 403
    assert (await gateway_client.put('/api/v1/gateway/model-pools/missing', json={'name':'Admin pool','purpose':'test','members':[{'model_id':'missing'}]})).status_code == 403


@pytest.mark.asyncio
async def test_pool_api_hides_other_users_pools_and_allows_same_name(gateway_client):
    ids = await _tiered_models(gateway_client, provider_name='local')
    payload = {'name':'My pool','purpose':'test','members':[{'model_id':ids[n]} for n in ('fast','balanced','reasoning')]}
    pool = await gateway_client.post('/api/v1/gateway/model-pools', json=payload)
    assert pool.status_code == 201
    async def other_user():
        return CurrentUser(id='user-2', email=None, roles=('user',), permissions=frozenset({'model_pools.read_own','model_pools.manage_own'}))
    app.dependency_overrides[get_current_user] = other_user
    assert (await gateway_client.get('/api/v1/gateway/model-pools')).json() == []
    assert (await gateway_client.get('/api/v1/gateway/model-pools/'+pool.json()['id'])).status_code == 404
    assert (await gateway_client.put('/api/v1/gateway/model-pools/'+pool.json()['id'],json=payload)).status_code == 404
    assert (await gateway_client.post('/api/v1/gateway/model-pools',json=payload)).status_code == 201
