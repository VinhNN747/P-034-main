import importlib

import pytest

from src.backend_ai.agents.routing.base import BaseRouter, RouteDecision
from src.backend_ai.services.internal_provider_clients import InternalProviderClients
from src.shared.config import Settings


def _factory():
    return importlib.import_module("src.backend_ai.gateway.factory")


class FakeJevRouter(BaseRouter):
    name = "jev"

    async def aroute(self, subtask, upstream_results=None, **kwargs):
        return RouteDecision(
            task_id=subtask.task_id,
            tier="small_fast",
            strategy=self.name,
            selected_model="fake-model",
        )


def test_offline_factory_registers_only_local_safe_strategies():
    settings = Settings(_env_file=None)
    factory = _factory()
    pipeline = factory.build_pipeline(settings=settings)
    orchestrator = factory.build_orchestrator(settings=settings, pipeline=pipeline)

    # "classifier" runs in-process (and falls back to the rules without a model), so it is local-safe.
    assert set(pipeline.understanders) == {"rule", "jev", "classifier"}
    assert pipeline.task_classifier_shadow is None  # no model configured
    assert set(pipeline.decomposers) == {"none", "deterministic"}
    assert set(orchestrator.routers) == {"deterministic"}
    assert set(orchestrator.aggregators) == {"none", "deterministic"}


def test_factory_registers_injected_live_adapters_without_network_calls():
    settings = Settings(
        _env_file=None,
        gateway_execution_mode="live",
        gateway_openai_enabled=True,
        gateway_jev_enabled=True,
    )
    fake_client = object()
    internal_clients = InternalProviderClients(
        llm_client=fake_client,
        jev_client=fake_client,
        split_gate_client=fake_client,
    )
    factory = _factory()
    pipeline = factory.build_pipeline(settings=settings, internal_clients=internal_clients)
    orchestrator = factory.build_orchestrator(
        settings=settings,
        pipeline=pipeline,
        internal_clients=internal_clients,
        jev_router=FakeJevRouter(),
    )

    assert "llm" in pipeline.understanders
    assert "llm" in pipeline.decomposers
    assert "jev" in orchestrator.routers
    assert "llm" in orchestrator.aggregators
    assert orchestrator.providers == {}


def test_unavailable_live_llm_strategy_raises_configuration_error():
    factory = _factory()
    with pytest.raises(factory.GatewayConfigurationError):
        factory.ensure_strategy_available("llm", Settings(_env_file=None))


def test_jev_analysis_can_degrade_to_rules_without_a_binding():
    factory = _factory()
    factory.ensure_strategy_available("jev", Settings(_env_file=None))
