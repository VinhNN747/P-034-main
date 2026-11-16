from src.backend.services.pool_rules import callable_provider_ids
from src.shared.config import Settings


def test_anthropic_pool_callable_gate_requires_live_mode_flag_and_configured_key():
    kinds = {"anthropic": "anthropic", "openai": "openai", "local": "local"}
    configured = {"anthropic", "openai"}
    disabled = Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", gateway_execution_mode="live",
        gateway_anthropic_enabled=False, gateway_openai_enabled=True)
    enabled = disabled.model_copy(update={"gateway_anthropic_enabled": True})
    offline = enabled.model_copy(update={"gateway_execution_mode": "offline"})

    assert callable_provider_ids(disabled, configured, kinds) == {"openai"}
    assert callable_provider_ids(enabled, configured, kinds) == {"anthropic", "openai"}
    assert callable_provider_ids(offline, configured, kinds) == set()
