from types import SimpleNamespace

import pytest

from src.backend_ai.services import internal_provider_clients as clients_module
from src.backend_ai.services.internal_provider_clients import build_internal_clients
from src.shared.config import Settings
from src.shared.services.provider_credential_resolver import CredentialUnavailableError


def _settings(**kwargs):
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite://",
        gateway_execution_mode="live",
        gateway_openai_enabled=True,
        gateway_jev_enabled=True,
        gateway_ocr_enabled=True,
        support_chat_llm_enabled=True,
        **kwargs,
    )


class FakeResolver:
    def __init__(self, credentials):
        self.credentials = credentials
        self.calls = []

    async def resolve_service(self, purpose):
        self.calls.append(purpose)
        credential = self.credentials.get(purpose)
        if credential is None:
            raise CredentialUnavailableError()
        return credential


def _credential(purpose, key, *, base_url=None):
    return SimpleNamespace(
        provider_id=f"{purpose}-connection",
        provider_name=f"{purpose}-provider",
        provider_kind="google" if purpose == "ocr_google" else "typesafe" if purpose == "routing_jev" else "openai",
        credential_id=f"{purpose}-credential",
        version=1,
        api_key=key,
        base_url=base_url,
    )


@pytest.mark.asyncio
async def test_jev_analysis_does_not_require_openai_target_or_internal_key(monkeypatch):
    resolver = FakeResolver({"routing_jev": _credential("routing_jev", "jev-secret")})
    monkeypatch.setattr(clients_module, "TypeSafeJevClient", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(clients_module, "TypeSafeSplitGateClient", lambda **kwargs: SimpleNamespace(**kwargs))

    result = await build_internal_clients(resolver, _settings(), need_jev=True)

    assert resolver.calls == ["routing_jev"]
    assert result.jev_client.api_key == "jev-secret"
    assert result.llm_client is None


@pytest.mark.asyncio
async def test_internal_binding_missing_never_picks_an_arbitrary_connection():
    resolver = FakeResolver({"target-openai": _credential("internal_openai", "unrelated-secret")})

    result = await build_internal_clients(resolver, _settings(), need_llm=True)

    assert resolver.calls == ["internal_openai"]
    assert result.llm_client is None
    assert result.unavailable_purposes == frozenset({"internal_openai"})
    assert "unrelated-secret" not in repr(result)


@pytest.mark.asyncio
async def test_ocr_uses_the_google_ocr_binding(monkeypatch):
    captured = {}

    def make_ocr(api_key, **kwargs):
        captured.update(api_key=api_key, **kwargs)
        return object()

    resolver = FakeResolver({"ocr_google": _credential("ocr_google", "ocr-secret")})
    monkeypatch.setattr(clients_module, "GeminiOcrClient", make_ocr)

    result = await build_internal_clients(resolver, _settings(), need_ocr=True)

    assert resolver.calls == ["ocr_google"]
    assert captured["api_key"] == "ocr-secret"
    assert result.ocr_client is not None


@pytest.mark.asyncio
async def test_llm_strategy_can_report_missing_internal_binding():
    resolver = FakeResolver({})

    result = await build_internal_clients(resolver, _settings(), need_llm=True)

    assert result.llm_client is None
    assert "internal_openai" in result.unavailable_purposes

    from src.backend_ai.gateway.factory import GatewayConfigurationError, ensure_strategy_available

    with pytest.raises(GatewayConfigurationError, match="internal provider binding"):
        ensure_strategy_available("llm", _settings(), result)


@pytest.mark.asyncio
async def test_support_client_uses_internal_openai_connection_metadata():
    resolver = FakeResolver(
        {"internal_openai": _credential("internal_openai", "support-secret", base_url="https://ai.example/v1")}
    )

    result = await build_internal_clients(resolver, _settings(), need_support=True)

    assert resolver.calls == ["internal_openai"]
    assert result.support_client.api_key == "support-secret"
    assert result.support_client.base_url == "https://ai.example/v1/"
    assert result.llm_client is None
