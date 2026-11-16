import pytest
from pydantic import ValidationError

from src.shared.config import Settings


def test_domain_database_uses_shared_supabase_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://supabase.example/auth")

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql://supabase.example/auth"
    assert not hasattr(settings, "app_database_url")


def test_gateway_defaults_to_deterministic_offline_execution():
    settings = Settings(_env_file=None)

    assert settings.support_chat_model == "gpt-5.6-luna"
    assert settings.support_chat_response_temperature == 0.45
    assert settings.support_chat_history_messages == 32
    assert settings.support_chat_context_chars == 32000
    assert settings.gateway_execution_mode == "offline"
    assert settings.gateway_default_router == "deterministic"
    assert settings.gateway_default_aggregator == "deterministic"
    assert settings.gateway_default_understander == "rule"
    assert settings.gateway_default_decomposer == "deterministic"
    assert settings.gateway_openai_enabled is False


def test_default_ocr_configuration_uses_current_flash_model_and_rates():
    settings = Settings(_env_file=None, database_url="postgresql://example.invalid/test")

    assert settings.gateway_ocr_model == "gemini-3.8-flash"
    assert settings.gateway_ocr_input_price_per_million_usd == settings.gateway_gemini_input_price_per_million_usd
    assert settings.gateway_ocr_output_price_per_million_usd == settings.gateway_gemini_output_price_per_million_usd


def test_anthropic_probe_model_environment_name_and_legacy_fallback(monkeypatch):
    monkeypatch.setenv("GATEWAY_ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("GATEWAY_ANTHROPIC_PROBE_MODEL", "claude-sonnet-5-5")

    settings = Settings(_env_file=None, database_url="postgresql://example.invalid/test")

    assert settings.gateway_anthropic_probe_model == "claude-sonnet-5-5"


def test_anthropic_probe_model_accepts_legacy_environment_name(monkeypatch):
    monkeypatch.setenv("GATEWAY_ANTHROPIC_MODEL", "claude-opus-5-5")

    settings = Settings(_env_file=None, database_url="postgresql://example.invalid/test")

    assert settings.gateway_anthropic_probe_model == "claude-opus-5-5"


def test_gateway_execution_mode_rejects_unknown_values():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, gateway_execution_mode="remote")


def test_demo_compatible_gateway_defaults_are_available():
    settings = Settings(_env_file=None)

    assert not hasattr(settings, "openai_api_key")
    assert not hasattr(settings, "typesafe_api_key")
    assert not hasattr(settings, "google_api_key")
    assert not hasattr(settings, "gemini_api_key")
    assert not hasattr(settings, "openrouter_api_key")
    assert settings.credential_encryption_key == ""
    assert not hasattr(settings, "credential_encryption_key_file")
    assert settings.gateway_jev_enabled is False
    assert settings.gateway_understander_model == "gpt-4o-mini"
    assert settings.gateway_decomposer_model == "gpt-4o-mini"
    assert settings.gateway_aggregator_model == "gpt-4o-mini"
    assert settings.gateway_model_tier_small_fast == "gpt-4o-mini"
    assert settings.gateway_model_tier_mid == "gpt-4o"
    assert settings.gateway_model_tier_large_reasoning == "o1"
    assert settings.gateway_routing_fallback_model == "gpt-4o-mini"
    assert settings.gateway_llm_timeout_seconds == 30.0
    assert settings.gateway_routing_timeout_seconds == 30.0


def test_demo_compatible_gateway_settings_map_from_environment(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-typesafe-key")
    monkeypatch.setenv("GATEWAY_JEV_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_UNDERSTANDER_MODEL", "understand-model")
    monkeypatch.setenv("GATEWAY_MODEL_TIER_MID", "mid-model")
    monkeypatch.setenv("GATEWAY_ROUTING_FALLBACK_MODEL", "fallback-model")
    monkeypatch.setenv("GATEWAY_LLM_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("GATEWAY_ROUTING_TIMEOUT_SECONDS", "9")

    settings = Settings(_env_file=None)

    assert not hasattr(settings, "typesafe_api_key")
    assert settings.gateway_jev_enabled is True
    assert settings.gateway_understander_model == "understand-model"
    assert settings.gateway_model_tier_mid == "mid-model"
    assert settings.gateway_routing_fallback_model == "fallback-model"
    assert settings.gateway_llm_timeout_seconds == 12.5
    assert settings.gateway_routing_timeout_seconds == 9.0


def test_existing_cors_and_supabase_settings_still_parse():
    settings = Settings(
        _env_file=None,
        cors_origins="http://localhost:3000",
        supabase_url="https://example.supabase.co",
        supabase_publishable_key="publishable-key",
    )

    assert settings.cors_origins == "http://localhost:3000"
    assert settings.supabase_url == "https://example.supabase.co"
    assert settings.supabase_publishable_key == "publishable-key"
