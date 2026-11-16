import pytest

pytest_plugins = ("tests.test_api.test_gateway_runtime",)


@pytest.mark.asyncio
async def test_observability_lists_are_paginated_and_incidents_are_mutable(runtime_client):
    client, application_id = runtime_client

    executed = await client.post(
        "/api/v1/gateway/requests",
        json={"application_id": application_id, "prompt": "hello", "aggregator": "none"},
    )
    assert executed.status_code == 200, executed.text

    requests = await client.get(
        "/api/v1/gateway/requests",
        params={"application_id": application_id, "page": 1, "page_size": 1},
    )
    assert requests.status_code == 200, requests.text
    assert requests.json()["total"] >= 1
    assert requests.json()["items"][0]["application_id"] == application_id
    assert "application_name" in requests.json()["items"][0]

    multi_sorted_requests = await client.get(
        "/api/v1/gateway/requests",
        params={"application_id": application_id, "sort_by": "status,latency", "sort_dir": "asc,desc"},
    )
    assert multi_sorted_requests.status_code == 200, multi_sorted_requests.text

    for path in ("/api/v1/gateway/summary", "/api/v1/gateway/usage", "/api/v1/gateway/incidents"):
        response = await client.get(path)
        assert response.status_code == 200, response.text
        assert {"items", "page", "page_size", "total"}.issubset(response.json())

    created = await client.post(
        "/api/v1/gateway/incidents",
        json={
            "application_id": application_id,
            "title": "Provider degraded",
            "description": "offline test incident",
            "severity": "warning",
        },
    )
    assert created.status_code == 201, created.text
    incident_id = created.json()["id"]

    updated = await client.patch(
        f"/api/v1/gateway/incidents/{incident_id}",
        json={"status": "resolved"},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "resolved"

    status = await client.get("/api/v1/gateway/status")
    assert status.status_code == 200
    assert status.json()["status"] == "ok"
