import pytest

from src.shared.dependencies.auth import CurrentUser, get_current_user
from tests.service_app import app


@pytest.mark.asyncio
async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_chat_empty_message(client):
    async def override_user():
        return CurrentUser(
            id="test-user",
            email="test@example.com",
            roles=("owner",),
            permissions=frozenset({"applications.read_own"}),
        )

    app.dependency_overrides[get_current_user] = override_user
    try:
        response = await client.post("/api/v1/chat", json={"message": ""})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 422  # Validation error


@pytest.mark.asyncio
async def test_agent_status(client):
    response = await client.get("/api/v1/status")
    assert response.status_code == 200
