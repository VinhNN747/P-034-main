import importlib
import importlib.util

import httpx
import pytest

from src.shared.config import Settings
from src.shared.dtos.support_chat import SupportChatResponse


def client_module():
    assert importlib.util.find_spec("src.backend.clients.ai_backend_client") is not None, (
        "Management needs an AI HTTP client"
    )
    return importlib.import_module("src.backend.clients.ai_backend_client")


@pytest.mark.asyncio
async def test_remote_support_uses_internal_ai_service(monkeypatch):
    module = client_module()
    from src.backend.services.remote_support_synthesizer import RemoteSupportResponseSynthesizer
    from src.backend_ai.dependencies import ai_service_auth
    from src.backend_ai.main import app
    from src.backend_ai.routers import ai_internal
    from src.shared.dtos.ai_internal import SupportGroundingVerdict, SupportRewriteOutput

    settings = Settings(_env_file=None, ai_service_token="service-secret", support_chat_llm_enabled=True)
    monkeypatch.setattr(ai_service_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(ai_internal, "get_settings", lambda: settings)

    class Provider:
        async def structured(self, *, schema, **kwargs):
            if schema is SupportGroundingVerdict:
                return SupportGroundingVerdict(supported=True)
            return SupportRewriteOutput(message="Verified remote answer", source_ids=[])

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from src.backend_ai.services.support_response_synthesizer import SupportResponseSynthesizer
    from src.shared.database.base import Base
    from src.shared.database.session import get_db

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db

    monkeypatch.setattr(
        SupportResponseSynthesizer,
        "from_settings",
        lambda _, **kwargs: SupportResponseSynthesizer(
            enabled=True, api_key="", model_id="test", timeout_seconds=1, client=Provider()
        ),
    )
    client = module.AIBackendClient(settings, transport=httpx.ASGITransport(app=app))
    result = await RemoteSupportResponseSynthesizer(client).synthesize(
        question="What happened?",
        response=SupportChatResponse(request_id="req-1", status="answered", message="Verified facts"),
        history=[],
    )
    assert result == "Verified remote answer"
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_ai_outage_preserves_verified_support_answer():
    module = client_module()
    from src.backend.services.remote_support_synthesizer import RemoteSupportResponseSynthesizer

    def unavailable(request):
        raise httpx.ConnectError("private-host secret", request=request)

    settings = Settings(_env_file=None, ai_service_token="service-secret", support_chat_llm_enabled=True)
    client = module.AIBackendClient(settings, transport=httpx.MockTransport(unavailable))
    result = await RemoteSupportResponseSynthesizer(client).synthesize(
        question="Question",
        response=SupportChatResponse(request_id="req-1", status="answered", message="Verified fallback"),
        history=[],
    )
    assert result == "Verified fallback"


@pytest.mark.asyncio
async def test_missing_service_token_does_not_make_http_request():
    module = client_module()

    def forbidden(request):
        pytest.fail("A request without configured service credentials must not leave management")

    client = module.AIBackendClient(
        Settings(_env_file=None, ai_service_token=""), transport=httpx.MockTransport(forbidden)
    )
    with pytest.raises(module.AIBackendUnavailableError, match="unavailable"):
        await client.post("/internal/v1/models/model-1/probe", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(401, {"detail": "private secret"}), (503, {}), (200, {"message": ""})])
async def test_invalid_remote_support_response_falls_back(status, body):
    module = client_module()
    from src.backend.services.remote_support_synthesizer import RemoteSupportResponseSynthesizer

    client = module.AIBackendClient(
        Settings(_env_file=None, ai_service_token="token", support_chat_llm_enabled=True),
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)),
    )
    response = SupportChatResponse(request_id="req-1", status="answered", message="Verified fallback")
    assert (
        await RemoteSupportResponseSynthesizer(client).synthesize(question="Question", response=response, history=[])
        == response.message
    )
