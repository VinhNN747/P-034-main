import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.backend_ai.services.openai_request_context import ensure_openai_request_id, install_openai_request_context


@pytest.mark.asyncio
async def test_request_id_exists_before_auth_and_validation():
    app = FastAPI()
    install_openai_request_context(app)

    @app.get("/v1/protected")
    async def protected(request: Request):
        assert ensure_openai_request_id(request)
        return JSONResponse({"error": {"code": "invalid_api_key"}}, status_code=401)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/protected")

    assert response.status_code == 401
    assert response.headers["x-request-id"]
    assert response.headers["x-gateway-request-id"] == response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_replay_keeps_operation_id_from_route_response():
    app = FastAPI()
    install_openai_request_context(app)

    @app.post("/v1/replay")
    async def replay(request: Request):
        ensure_openai_request_id(request)
        return JSONResponse({"id": "stored-operation"}, headers={"x-request-id": "stored-operation"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/replay")

    assert response.headers["x-request-id"] == "stored-operation"
    assert response.headers["x-gateway-request-id"] == "stored-operation"


@pytest.mark.asyncio
async def test_unhandled_error_is_sanitized_with_id_before_response_start():
    app = FastAPI()
    install_openai_request_context(app)
    @app.get('/v1/failure')
    async def failure():
        raise RuntimeError('private storage credential')
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test') as client:
        response = await client.get('/v1/failure')
    assert response.status_code == 500
    assert response.json()['error']['code'] == 'internal_error'
    assert response.headers['x-request-id']
    assert response.headers['x-should-retry'] == 'false'
    assert 'private' not in response.text
