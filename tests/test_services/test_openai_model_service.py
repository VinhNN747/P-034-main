from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _row(name, *, model_status="active", provider_status="active", provider_name="provider-a", provider_kind="openai"):
    model = SimpleNamespace(
        id=f"model-{name}-{provider_name}", name=name, status=model_status,
        capabilities_json=["chat"], created_at=datetime(2026, 10, 8, tzinfo=UTC),
    )
    provider = SimpleNamespace(id=provider_name, name=provider_name, kind=provider_kind, purpose="target",
        status=provider_status)
    return (None, model, provider)


def _principal(application_id="app-a"):
    return SimpleNamespace(application_id=application_id)


def _service(monkeypatch, rows, *, config=SimpleNamespace(model_pool_id="pool-a"), settings=None,
             configured_ids=None):
    from src.backend_ai.services import openai_model_service

    session = SimpleNamespace(scalar=AsyncMock(return_value=config))
    monkeypatch.setattr(openai_model_service, "routing_candidates", AsyncMock(return_value=rows))

    if configured_ids is not None:
        class CredentialRepository:
            def __init__(self, _session):
                pass

            async def configured_provider_ids(self):
                return set(configured_ids)

        monkeypatch.setattr(openai_model_service, "ProviderCredentialRepository", CredentialRepository)
    return openai_model_service.OpenAIModelService(session, settings=settings)


@pytest.mark.asyncio
async def test_retrieve_returns_the_exact_card_from_visible_list(monkeypatch):
    service = _service(monkeypatch, [_row("vendor/model-a")])
    cards = await service.list_visible(_principal())

    retrieved = await service.retrieve_visible(_principal(), "vendor/model-a")
    assert retrieved.created == int(datetime(2026, 10, 8, tzinfo=UTC).timestamp())

    assert retrieved.model_dump() == next(
        card.model_dump() for card in cards.data if card.id == "vendor/model-a"
    )


@pytest.mark.asyncio
async def test_unknown_inactive_and_foreign_models_share_generic_not_found(monkeypatch):
    from src.backend_ai.routers.openai_errors import OpenAIAPIError

    service = _service(monkeypatch, [_row("visible"), _row("inactive", model_status="inactive")])
    errors = []
    for model_id in ("unknown", "inactive", "other-app-model"):
        with pytest.raises(OpenAIAPIError) as caught:
            await service.retrieve_visible(_principal(), model_id)
        errors.append(caught.value)

    assert {(error.status_code, error.type, error.code, error.param) for error in errors} == {
        (404, "invalid_request_error", "model_not_found", "model")
    }


@pytest.mark.asyncio
async def test_unconfigured_application_keeps_alias_only_visibility(monkeypatch):
    from src.backend_ai.gateway.model_alias import ALIAS_DESCRIPTIONS

    service = _service(monkeypatch, [_row("global-model")], config=None)

    cards = await service.list_visible(_principal())

    assert [card.id for card in cards.data] == list(ALIAS_DESCRIPTIONS)


@pytest.mark.asyncio
async def test_duplicate_provider_names_create_one_public_card_but_keep_candidates(monkeypatch):
    rows = [_row("shared-name", provider_name="provider-a"), _row("shared-name", provider_name="provider-b")]
    service = _service(monkeypatch, rows)

    cards = await service.list_visible(_principal())
    candidates = await service.visible_candidates(_principal())

    assert [card.id for card in cards.data].count("shared-name") == 1
    assert len([row for row in candidates if row[0].name == "shared-name"]) == 2


@pytest.mark.asyncio
async def test_alias_id_with_colon_and_reserved_alias_collision(monkeypatch):
    from src.backend_ai.gateway.model_alias import ALIAS_DESCRIPTIONS

    service = _service(monkeypatch, [_row("auto"), _row("auto:fast"), _row("vendor:model")])

    cards = await service.list_visible(_principal())

    assert next(card.owned_by for card in cards.data if card.id == "auto") == "gateway-router"
    assert next(card.owned_by for card in cards.data if card.id == "auto:fast") == "gateway-router"
    assert next(card.owned_by for card in cards.data if card.id == "vendor:model") == "gateway-pinned"
    assert set(ALIAS_DESCRIPTIONS).issubset({card.id for card in cards.data})


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled, configured, visible", [(True, True, True), (False, True, False),
    (True, False, False)])
async def test_anthropic_sdk_model_list_requires_runtime_flag_and_configured_credential(monkeypatch,
                                                                                       enabled, configured, visible):
    from src.shared.config import Settings

    provider_id = "anthropic-provider"
    service = _service(monkeypatch, [_row("claude-haiku-5-5", provider_name=provider_id,
        provider_kind="anthropic")], settings=Settings(_env_file=None,
            database_url="sqlite+aiosqlite:///:memory:", gateway_execution_mode="live",
            gateway_anthropic_enabled=enabled), configured_ids={provider_id} if configured else set())

    cards = await service.list_visible(_principal())

    assert ("claude-haiku-5-5" in {card.id for card in cards.data}) is visible
