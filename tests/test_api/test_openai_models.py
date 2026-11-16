from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport


@pytest.mark.asyncio
async def test_models_route_retrieves_the_same_encoded_card_including_slash_ids(monkeypatch):
    from src.backend_ai.dependencies.openai_auth import get_openai_principal
    from src.backend_ai.routers import openai_compat
    from src.backend_ai.routers.openai_compat import router
    from src.backend_ai.routers.openai_errors import OpenAIAPIError, openai_error_handler
    from src.backend_ai.services import openai_model_service
    from src.shared.config import Settings
    from src.shared.database.session import get_db

    model = SimpleNamespace(
        id="model-1",
        name="provider/model",
        status="active",
        capabilities_json=["chat"],
        created_at=datetime(2026, 10, 8, tzinfo=UTC),
    )
    provider = SimpleNamespace(name="provider", status="active", kind="offline", purpose="target")
    monkeypatch.setattr(
        openai_model_service,
        "get_settings",
        lambda: Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", gateway_execution_mode="offline"),
    )
    monkeypatch.setattr(openai_model_service, "routing_candidates", AsyncMock(return_value=[(None, model, provider)]))
    monkeypatch.setattr(
        openai_compat, "routing_candidates", AsyncMock(return_value=[(None, model, provider)]), raising=False
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(model_pool_id="pool-1")))
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(OpenAIAPIError, openai_error_handler)

    async def principal_override():
        return SimpleNamespace(application_id="app-1")

    app.dependency_overrides[get_openai_principal] = principal_override

    async def database_override():
        yield session

    app.dependency_overrides[get_db] = database_override
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway.test") as transport:
        listed = await transport.get("/v1/models")
        retrieved = await transport.get("/v1/models/provider%2Fmodel")

    assert listed.status_code == retrieved.status_code == 200
    listed_card = next(card for card in listed.json()["data"] if card["id"] == "provider/model")
    assert retrieved.json() == listed_card
