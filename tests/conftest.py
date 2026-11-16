import os
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test:test@localhost:5432/test",
)
os.environ["SUPPORT_CHAT_LLM_ENABLED"] = "false"

from tests.service_app import app


@pytest.fixture(autouse=True)
def internal_ai_transport(monkeypatch):
    """Keep management -> AI HTTP calls real without using live model services."""
    from src.backend.clients.ai_backend_client import AIBackendClient, get_ai_backend_client
    from src.backend_ai.dependencies import ai_service_auth
    from src.backend_ai.main import app as ai_app
    from src.backend_ai.routers import ai_internal
    from src.shared.config import Settings

    settings = Settings(
        _env_file=None, ai_service_token="test-service-token",
        gateway_execution_mode="offline", support_chat_llm_enabled=False,
    )
    monkeypatch.setattr(ai_service_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(ai_internal, "get_settings", lambda: settings)
    app.dependency_overrides[get_ai_backend_client] = lambda: AIBackendClient(
        settings, transport=ASGITransport(app=ai_app)
    )
    yield
    app.dependency_overrides.pop(get_ai_backend_client, None)


@pytest_asyncio.fixture
async def client():
    """Async HTTP client for testing API endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def mock_llm():
    """Mock LLM to avoid calling OpenAI during tests.

    Usage in test:
        def test_something(mock_llm):
            # LLM calls will return mock response instead of hitting OpenAI
            ...
    """
    mock = AsyncMock()
    mock.ainvoke.return_value = AsyncMock(content="Mocked LLM response")
    return mock


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    """The gateway circuit breaker and file storage are process-wide; isolate
    tests from them. Only touches the AI factory if a test already imported
    it, so management-only tests never load the AI runtime."""
    import sys

    factory = sys.modules.get("src.backend_ai.gateway.factory")
    if factory is not None:
        factory._CIRCUIT_BREAKER = None
        factory._FILE_STORAGE = None
    yield
    factory = sys.modules.get("src.backend_ai.gateway.factory")
    if factory is not None:
        factory._CIRCUIT_BREAKER = None
        factory._FILE_STORAGE = None
