import pytest

from src.backend_ai.routers.gateway_runtime import GatewayRuntimeInput, _validate_strategies


def test_runtime_defaults_to_auto_aggregator_and_accepts_explicit_modes():
    payload = GatewayRuntimeInput(application_id="app-1", prompt="hello")

    assert payload.aggregator == "auto"
    _validate_strategies(payload)
    _validate_strategies(payload.model_copy(update={"aggregator": "none"}))
    _validate_strategies(payload.model_copy(update={"aggregator": "deterministic"}))
    _validate_strategies(payload.model_copy(update={"aggregator": "llm"}))


def test_runtime_contract_rejects_unknown_aggregator():
    payload = GatewayRuntimeInput(application_id="app-1", prompt="hello", aggregator="missing")

    with pytest.raises(Exception, match="Unknown aggregator 'missing'"):
        _validate_strategies(payload)
