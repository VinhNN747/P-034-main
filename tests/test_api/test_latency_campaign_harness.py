from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from src.shared.models.latency import LatencyObservationRecord
from tests.latency_support import count_latency_observations, wait_for_campaign

pytest_plugins = ("tests.latency_support",)


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.asyncio
async def test_live_auto_harness_collects_playground_and_sdk_attempts_in_isolated_database(
    latency_campaign_env,
):
    env = latency_campaign_env
    client = env["client"]
    assert env["runtime"].config.mode == "auto"
    assert env["runtime"].session_factory is env["factory"]
    assert env["runtime"].model_store.snapshot() is None
    assert await count_latency_observations(env["factory"]) == 0

    model_list = await client.get("/v1/models", headers=_auth(env["key"]))
    assert model_list.status_code == 200, model_list.text
    assert await count_latency_observations(env["factory"]) == 0

    playground = await client.post(
        "/api/v1/gateway/requests",
        headers=_auth(env["key"]),
        json={
            "application_id": env["application_id"],
            "prompt": "campaign playground marker",
            "source": "playground",
            "router": "database",
            "understander": "rule",
            "decomposer": "deterministic",
            "aggregator": "deterministic",
        },
    )
    assert playground.status_code == 200, playground.text
    playground_body = playground.json()
    assert playground_body["status"] == "completed", (
        f"status={playground_body.get('status')} error_code={playground_body.get('error_code')} "
        f"upstream_calls={len(env['calls'])}"
    )

    sdk = await client.post(
        "/v1/chat/completions",
        headers=_auth(env["key"]),
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "campaign sdk marker"}],
            "temperature": 0.3,
        },
    )
    assert sdk.status_code == 200, sdk.text
    assert sdk.json()["choices"][0]["message"]["content"] == "mock upstream response"

    await wait_for_campaign(lambda: _has_two_rows(env["factory"]))
    await wait_for_campaign(lambda: _has_persisted(env["runtime"], 2))
    async with env["factory"]() as session:
        rows = list((await session.scalars(
            select(LatencyObservationRecord).order_by(LatencyObservationRecord.dispatched_at)
        )).all())

    assert len(rows) == 2
    assert {row.source for row in rows} == {"playground", "openai_sdk"}
    assert {row.mode for row in rows} == {"auto"}
    assert {row.execution_mode for row in rows} == {"live"}
    assert {row.status for row in rows} == {"success"}
    assert {row.catalog_model_id for row in rows} == {env["model_id"]}
    assert all(row.provider_id == env["provider_id"] for row in rows)
    assert all(row.upstream_model_name == "mock-chat-model" for row in rows)
    assert all(row.provider_total_ms is not None and row.provider_total_ms > 0 for row in rows)
    assert all(row.features_json for row in rows)
    stored = json.dumps([row.features_json for row in rows])
    assert "campaign playground marker" not in stored
    assert "campaign sdk marker" not in stored
    assert "latency-campaign-upstream-key" not in stored
    assert len(env["calls"]) == 2
    assert all(call["path"].endswith("/chat/completions") for call in env["calls"])


@pytest.mark.asyncio
async def test_campaign_lifespan_starts_writer_refresh_and_scheduler(latency_campaign_env):
    import asyncio

    env = latency_campaign_env
    assert env["runtime"].collector.queue.empty()
    assert hasattr(env["runtime"], "history_cache")
    task_names = {task.get_name() for task in asyncio.all_tasks()}
    assert "adaptive-latency-worker" in task_names


async def _has_two_rows(factory) -> bool:
    return await count_latency_observations(factory) >= 2


async def _has_persisted(runtime, expected: int) -> bool:
    return runtime.collector.metrics["persisted"] >= expected
