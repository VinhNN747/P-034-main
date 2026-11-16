import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from src.shared.dtos.runtime_api import (
    RequestConstraints,
    RuntimeAPIError,
    RuntimeRequestInput,
    runtime_api_error_handler,
)


def test_public_request_rejects_client_owned_routing_fields():
    with pytest.raises(ValidationError):
        RuntimeRequestInput.model_validate({"prompt": "hello", "application_id": "another-app"})
    with pytest.raises(ValidationError):
        RuntimeRequestInput.model_validate({"prompt": "hello", "router": "deterministic"})
    with pytest.raises(ValidationError):
        RuntimeRequestInput.model_validate({"prompt": "hello", "source": "playground"})


def test_public_constraints_enforce_caps_and_forbid_unknown_options():
    assert RequestConstraints.model_validate({"max_output_tokens": 32768}).max_output_tokens == 32768
    with pytest.raises(ValidationError):
        RequestConstraints.model_validate({"max_output_tokens": 32769})
    with pytest.raises(ValidationError):
        RequestConstraints.model_validate({"temperature": 0.2})


@pytest.mark.asyncio
async def test_runtime_error_handler_returns_stable_envelope_and_correlation_header():
    app = FastAPI()
    app.add_exception_handler(RuntimeAPIError, runtime_api_error_handler)

    @app.get("/runtime")
    async def runtime():
        raise RuntimeAPIError(401, "AUTHENTICATION_FAILED", "A valid gateway API key is required.",
                              headers={"WWW-Authenticate": "Bearer"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/runtime")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["x-request-id"] == response.json()["request_id"]
    assert response.json()["error"] == {
        "code": "AUTHENTICATION_FAILED",
        "message": "A valid gateway API key is required.",
        "retryable": False,
        "details": {},
    }
