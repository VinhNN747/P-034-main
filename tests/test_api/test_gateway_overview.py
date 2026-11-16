import pytest

pytest_plugins = ("tests.test_api.test_gateway_runtime",)


@pytest.mark.asyncio
async def test_gateway_overview_aggregates_persisted_runtime_request(runtime_client):
    client, application_id = runtime_client
    executed = await client.post(
        "/api/v1/gateway/requests",
        json={
            "application_id": application_id,
            "prompt": "hello overview",
            "source": "playground",
            "aggregator": "none",
        },
    )
    assert executed.status_code == 200, executed.text

    response = await client.get(
        "/api/v1/gateway/overview",
        params={"application_id": application_id, "scope": "own"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_requests"] == 1
    assert body["success_rate"] == 1.0
    assert body["average_latency_ms"] >= 0
    assert body["total_cost"] >= 0
    assert body["active_model_count"] == 1
    assert body["recent_signals"]
    assert body["from"] is None
    assert body["to"] is None


@pytest.mark.asyncio
async def test_gateway_overview_global_scope_requires_existing_permission(runtime_client):
    client, application_id = runtime_client
    response = await client.get(
        "/api/v1/gateway/overview",
        params={"application_id": application_id, "scope": "global"},
    )
    assert response.status_code == 403
