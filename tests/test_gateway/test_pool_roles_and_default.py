import pytest
from sqlalchemy import select

from src.backend_ai.services.gateway_routing_service import DatabaseRoutingService
from src.shared.dtos.gateway import RequestConstraints
from src.shared.models.gateway import (
    ApplicationGatewayConfig,
    Model,
    ModelPool,
    ModelPoolMember,
    Provider,
    RoutingPolicy,
)
from src.shared.repositories.routing_catalog import DEFAULT_POOL_PURPOSE
from tests.test_gateway.test_jev_db_routing_and_decomposition import _task
from tests.test_gateway.test_routing_service import routing_context  # noqa: F401 - fixture


async def _ladder(session, context):
    """cheap = small, quality = mid, blocked = large; plus a cheaper, faster small model
    on the other provider that the ranking alone would prefer over cheap."""
    for key, label in {"cheap": "simple", "quality": "mid", "blocked": "hard"}.items():
        (await session.get(Model, context[key])).quality_tier = label
    external = await session.scalar(select(Provider).where(Provider.name == "external"))
    spare = Model(provider_id=external.id, name="spare-small", display_name="Spare small", status="active",
                  capabilities_json=["chat", "classification", "text"], quality_tier="simple",
                  target_latency_ms=10, input_price_per_million=0.01, output_price_per_million=0.02)
    session.add(spare)
    await session.flush()
    policy = await session.get(RoutingPolicy, (await _config(session, context["application_id"])).routing_policy_id)
    policy.guardrails_json = {"quality_scores": {**policy.guardrails_json["quality_scores"], spare.id: 100}}
    await session.commit()
    return spare.id


async def _config(session, app):
    return await session.scalar(select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == app))


@pytest.mark.asyncio
async def test_the_primary_of_a_tier_goes_before_its_fallback_then_the_next_tier(routing_context):  # noqa: F811
    session, app = routing_context["session"], routing_context["application_id"]
    spare = await _ladder(session, routing_context)
    config = await _config(session, app)
    session.add(ModelPoolMember(pool_id=config.model_pool_id, model_id=spare, role="fallback"))
    await session.commit()

    decision = await DatabaseRoutingService(session, app).route(_task())
    assert decision.selected_model == routing_context["cheap"]
    assert decision.fallback_models[0] == spare
    assert decision.fallback_models[1] == routing_context["quality"]

    # The fallback serves the tier when its primary cannot.
    blocked = RequestConstraints(blocked_models={routing_context["cheap"]})
    assert (await DatabaseRoutingService(session, app).route(_task(), blocked)).selected_model == spare


@pytest.mark.asyncio
async def test_apps_without_a_pool_auto_route_on_the_default_pool_but_pinned_models_still_work(routing_context):  # noqa: F811
    session, app = routing_context["session"], routing_context["application_id"]
    spare = await _ladder(session, routing_context)
    config = await _config(session, app)
    config.model_pool_id = None
    default = ModelPool(owner_user_id=None, name="Default", purpose=DEFAULT_POOL_PURPOSE, status="active")
    session.add(default)
    await session.flush()
    for key in ("cheap", "quality", "blocked"):
        session.add(ModelPoolMember(pool_id=default.id, model_id=routing_context[key], role="primary"))
    await session.commit()

    auto = await DatabaseRoutingService(session, app).route(_task())
    assert auto.selected_model == routing_context["cheap"]
    assert spare not in auto.fallback_models
    assert "default pool" in auto.reason

    # A model outside the default pool, asked for by name, is still served from the catalog.
    pinned = await DatabaseRoutingService(session, app).route(_task(), RequestConstraints(allowed_models={spare}))
    assert pinned.selected_model == spare
    assert "default pool" not in pinned.reason

    # An inactive default pool means the whole catalog again.
    default.status = "inactive"
    await session.commit()
    whole = await DatabaseRoutingService(session, app).route(_task())
    assert "default pool" not in whole.reason
    assert spare in [whole.selected_model, *whole.fallback_models]


@pytest.mark.asyncio
async def test_an_app_pool_ignores_the_default_pool(routing_context):  # noqa: F811
    session, app = routing_context["session"], routing_context["application_id"]
    await _ladder(session, routing_context)
    default = ModelPool(owner_user_id=None, name="Default", purpose=DEFAULT_POOL_PURPOSE, status="active")
    session.add(default)
    await session.flush()
    session.add(ModelPoolMember(pool_id=default.id, model_id=routing_context["quality"], role="primary"))
    await session.commit()
    decision = await DatabaseRoutingService(session, app).route(_task())
    assert decision.selected_model == routing_context["cheap"]
    assert "default pool" not in decision.reason


async def _system_pool(session, purpose, members):
    pool = ModelPool(owner_user_id=None, name=purpose, purpose=purpose, status="active")
    session.add(pool)
    await session.flush()
    for model_id in members:
        session.add(ModelPoolMember(pool_id=pool.id, model_id=model_id, role="primary"))
    await session.commit()
    return pool


@pytest.mark.asyncio
async def test_a_pool_without_a_three_tier_ladder_auto_routes_on_the_default_pool(routing_context):  # noqa: F811
    session, app = routing_context["session"], routing_context["application_id"]
    spare = await _ladder(session, routing_context)
    config = await _config(session, app)
    # The application's pool loses its large model: no longer one primary per tier.
    own_large = await session.scalar(select(ModelPoolMember).where(
        ModelPoolMember.pool_id == config.model_pool_id, ModelPoolMember.model_id == routing_context["blocked"]))
    await session.delete(own_large)
    await _system_pool(session, DEFAULT_POOL_PURPOSE, [spare, routing_context["quality"], routing_context["blocked"]])

    auto = await DatabaseRoutingService(session, app).route(_task())
    assert auto.selected_model == spare
    assert "default pool (application pool has no 3-tier ladder)" in auto.reason

    # A model of the application's own pool, pinned by name, still works.
    pinned = RequestConstraints(allowed_models={routing_context["cheap"]})
    decision = await DatabaseRoutingService(session, app).route(_task(), pinned)
    assert decision.selected_model == routing_context["cheap"]
    assert "default pool" not in decision.reason


@pytest.mark.asyncio
async def test_auto_code_routes_on_the_code_template_and_falls_back_to_the_default(routing_context):  # noqa: F811
    from src.backend_ai.services.gateway_routing_service import DatabaseRoutingSnapshot
    from src.shared.repositories.routing_catalog import routing_set, system_pool_purpose

    session, app = routing_context["session"], routing_context["application_id"]
    spare = await _ladder(session, routing_context)
    config = await _config(session, app)
    config.model_pool_id = None
    await session.commit()
    await _system_pool(session, DEFAULT_POOL_PURPOSE, [routing_context[k] for k in ("cheap", "quality", "blocked")])
    await _system_pool(session, system_pool_purpose("code"), [spare, routing_context["quality"], routing_context["blocked"]])

    async def route(template):
        routing = await routing_set(session, None, template=template)
        policy = await session.get(RoutingPolicy, config.routing_policy_id)
        snapshot = DatabaseRoutingSnapshot(config=config, policy=policy, candidates=tuple(routing.rows), routing=routing)
        return await DatabaseRoutingService(session, app, snapshot=snapshot).route(_task())

    code = await route("code")
    assert code.selected_model == spare and "template pool code" in code.reason
    plain = await route(None)
    assert plain.selected_model == routing_context["cheap"] and "default pool" in plain.reason
    # A template that does not exist (or is inactive) means the default pool.
    missing = await route("docs")
    assert missing.selected_model == routing_context["cheap"] and "default pool" in missing.reason


@pytest.mark.asyncio
async def test_an_application_pool_with_a_ladder_is_not_widened_by_an_alias(routing_context):  # noqa: F811
    from src.shared.repositories.routing_catalog import routing_set, system_pool_purpose

    session, app = routing_context["session"], routing_context["application_id"]
    spare = await _ladder(session, routing_context)
    await _system_pool(session, system_pool_purpose("code"), [spare, routing_context["quality"], routing_context["blocked"]])
    routing = await routing_set(session, (await _config(session, app)).model_pool_id, template="code")
    assert routing.auto_roles == {}
    assert spare not in {model.id for _, model, _ in routing.rows}
    assert "auto:code not applied" in routing.note


@pytest.mark.asyncio
async def test_any_owner_can_select_a_system_template_for_an_application(routing_context):  # noqa: F811
    from src.backend.dtos.gateway_configuration import GatewayConfigUpsert
    from src.backend.services.gateway_configuration_service import GatewayConfigurationService
    from src.shared.repositories.routing_catalog import system_pool_purpose

    session, app = routing_context["session"], routing_context["application_id"]
    spare = await _ladder(session, routing_context)
    template = await _system_pool(
        session, system_pool_purpose("code"), [spare, routing_context["quality"], routing_context["blocked"]])
    await GatewayConfigurationService(session).upsert("owner-1", app, GatewayConfigUpsert(model_pool_id=template.id))
    decision = await DatabaseRoutingService(session, app).route(_task())
    assert decision.selected_model == spare


def test_template_aliases():
    from src.backend_ai.gateway.model_alias import resolve_model, template_for_alias

    assert template_for_alias("auto:code") == "code"
    assert template_for_alias(" AUTO:CODE ") == "code"
    assert template_for_alias("auto") is None and template_for_alias("gpt-4o-mini") is None
    assert template_for_alias(None) is None
    selection = resolve_model("auto:code")
    assert not selection.is_pinned and selection.quality_priority is None


@pytest.mark.asyncio
async def test_system_pools_are_listed_read_only_for_every_owner(routing_context):  # noqa: F811
    from src.backend.dtos.catalog import ModelPoolMemberCreate, ModelPoolUpdate
    from src.backend.services.catalog_service import CatalogReferenceError, CatalogService
    from src.shared.repositories.routing_catalog import system_pool_purpose

    session = routing_context["session"]
    spare = await _ladder(session, routing_context)
    template = await _system_pool(
        session, system_pool_purpose("code"), [spare, routing_context["quality"], routing_context["blocked"]])
    catalog = CatalogService(session)
    for owner in ("owner-1", "someone-else"):
        listed = {pool.id: pool for pool in await catalog.list_pools(owner)}
        assert template.id in listed and listed[template.id].owner_user_id is None
        assert (await catalog.get_pool(template.id, owner)).id == template.id
    with pytest.raises(CatalogReferenceError, match="not found"):
        await catalog.update_pool(template.id, ModelPoolUpdate(
            name="mine", purpose="x", members=[ModelPoolMemberCreate(model_id=spare)]), "owner-1")
