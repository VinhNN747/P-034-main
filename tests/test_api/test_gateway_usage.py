import csv
import io

import pytest

pytest_plugins = ("tests.test_api.test_gateway_runtime",)


@pytest.mark.asyncio
async def test_gateway_usage_applies_application_model_provider_source_status_and_date_filters(
    runtime_client,
):
    client, application_id = runtime_client
    executed = await client.post(
        "/api/v1/gateway/requests",
        json={
            "application_id": application_id,
            "prompt": "hello usage",
            "source": "playground",
            "aggregator": "none",
        },
    )
    assert executed.status_code == 200, executed.text

    response = await client.get(
        "/api/v1/gateway/usage",
        params={
            "application_id": application_id,
            "model_id": "local-deterministic",
            "provider_id": "offline",
            "source": "playground",
            "status": "completed",
            "from": "2000-01-01T00:00:00+00:00",
            "to": "2100-01-01T00:00:00+00:00",
            "page": 1,
            "page_size": 10,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["application_id"] == application_id
    assert row["status"] == "completed"
    assert row["source"] == "playground"
    assert row["request_id"] == executed.json()["request_id"]
    assert row["tokens"] >= 0
    assert "latency_ms" in row
    assert "fallback_used" in row
    assert body["summary"]["requests"] == 1

    empty = await client.get(
        "/api/v1/gateway/usage",
        params={"application_id": application_id, "from": "2100-01-01T00:00:00+00:00"},
    )
    assert empty.status_code == 200
    assert empty.json()["total"] == 0


@pytest.mark.asyncio
async def test_gateway_usage_csv_export_uses_the_same_authorized_filters(runtime_client):
    client, application_id = runtime_client
    executed = await client.post(
        "/api/v1/gateway/requests",
        json={"application_id": application_id, "prompt": "hello csv", "source": "api", "aggregator": "none"},
    )
    assert executed.status_code == 200, executed.text

    response = await client.get(
        "/api/v1/gateway/usage/export.csv",
        params={"application_id": application_id, "source": "api"},
    )

    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    assert rows[0]["request_id"] == executed.json()["request_id"]
    assert rows[0]["source"] == "api"
