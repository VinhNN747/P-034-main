from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import func, select

from src.backend_ai.latency.contracts import LatencyFeatures, ModelKey, thaw
from src.backend_ai.latency.predictor import LatencyResolver
from src.backend_ai.latency.store import validate_artifact
from src.shared.models.latency import LatencyObservationRecord, LatencyTrainingRunRecord
from tests.latency_support import wait_for_campaign
from tests.service_app import app

pytest_plugins = ("tests.latency_support",)


CASES = ("native", "chat", "stream", "tools", "schema", "n2")


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _case_key(row: LatencyObservationRecord) -> str:
    values = row.features_json
    if values["protocol"] == "native":
        return "native"
    if values["streaming"]:
        return "stream"
    if values["has_tools"]:
        return "tools"
    if values["has_schema"]:
        return "schema"
    if values["n"] == 2:
        return "n2"
    return "chat"


def _assert_live_artifact_changes_ranking(env, artifact, observed_row):
    from src.backend_ai.services.gateway_routing_service import DatabaseRoutingService, _Candidate
    from src.shared.dtos.gateway import Difficulty, QualityPriority, SubTask, TaskType

    key = ModelKey(
        observed_row.catalog_model_id,
        observed_row.provider_id,
        observed_row.upstream_model_name,
        observed_row.connection_fingerprint,
        observed_row.provider_kind,
    )
    features = LatencyFeatures(
        key,
        observed_row.profile_key,
        datetime.now(UTC),
        observed_row.features_json,
    )
    resolver = LatencyResolver(artifact, env["runtime"].history_cache.snapshot(), env["runtime"].config)
    resolution = resolver.resolve(features, catalog_ms=900.0, now=datetime.now(UTC))
    assert resolution.source == "predicted", (resolution.source, resolution.gate_reason)
    assert resolution.effective_ms is not None and resolution.effective_ms < 899

    trained_id = env["model_id"]
    unseen_id = "unseen-ranking-candidate"
    provider = SimpleNamespace(
        id=env["provider_id"], name="mock-openai", kind="openai", purpose="target", status="active"
    )

    def candidate(model_id: str, catalog_ms: float):
        model = SimpleNamespace(
            id=model_id,
            name="mock-chat-model" if model_id == trained_id else unseen_id,
            input_price_per_million=1.0,
            output_price_per_million=0.0,
            pricing_version="test",
            target_latency_ms=catalog_ms,
            quality_tier="mid",
            capabilities_json=["chat"],
            status="active",
            context_window=16_000,
        )
        return _Candidate(model, provider)

    trained_candidate = candidate(trained_id, 900.0)
    unseen_candidate = candidate(unseen_id, min(899.0, resolution.effective_ms + 100.0))
    candidates = [trained_candidate, unseen_candidate]
    task = SubTask(
        task_id="latency-ranking-acceptance",
        description="rank two eligible mock chat candidates",
        task_type=TaskType.CLASSIFICATION,
        difficulty=Difficulty.MEDIUM,
        required_capabilities=["chat"],
        expected_output="label",
    )
    catalog_order = DatabaseRoutingService._rank_candidates(
        candidates, task, QualityPriority.LATENCY_FIRST, {}
    )
    predicted_order = DatabaseRoutingService._rank_candidates(
        candidates,
        task,
        QualityPriority.LATENCY_FIRST,
        {},
        latency_overrides={trained_id: resolution.effective_ms},
    )
    assert catalog_order[0].model.id == unseen_id
    assert predicted_order[0].model.id == trained_id
    assert trained_candidate.model.quality_tier == unseen_candidate.model.quality_tier
    return {
        "catalog_winner": catalog_order[0].model.id,
        "predicted_winner": predicted_order[0].model.id,
        "trained_prediction_ms": round(resolution.effective_ms, 3),
        "unseen_candidate_catalog_ms": round(unseen_candidate.latency, 3),
    }


async def _rows(factory) -> list[LatencyObservationRecord]:
    async with factory() as session:
        return list((await session.scalars(
            select(LatencyObservationRecord).order_by(LatencyObservationRecord.dispatched_at)
        )).all())


async def _runs(factory) -> list[LatencyTrainingRunRecord]:
    async with factory() as session:
        return list((await session.scalars(
            select(LatencyTrainingRunRecord).order_by(LatencyTrainingRunRecord.created_at)
        )).all())


def _message(size: int, marker: str = "") -> list[dict]:
    return [{"role": "user", "content": f"{marker}{'x' * size}"}]


async def _send_case(env, client, case: str, size: int, *, marker: str = "", surface: str | None = None):
    key = env["key"]
    body_marker = marker or f"campaign-{case}-{size}-"
    if case == "native":
        text = f"{body_marker}{'x' * size}"
        if surface is None:
            surface = "playground" if (size // 70) % 2 == 0 else "runtime"
        if surface == "playground":
            messages = [{"role": "user", "content": text}]
            if marker.startswith("playground-pdf-context-"):
                import base64

                from tests.test_gateway.test_multimodal import _pdf

                pdf = base64.b64encode(_pdf(["PDF_CONTEXT_SURROGATE " + "p" * max(80, size)])).decode("ascii")
                messages = [{"role": "user", "content": [
                    {"type": "text", "text": text},
                    {"type": "file", "file": {
                        "filename": "campaign-context.pdf",
                        "file_data": f"data:application/pdf;base64,{pdf}",
                    }},
                ]}]
            response = await env["client"].post(
                "/api/v1/gateway/requests",
                json={
                    "application_id": env["application_id"],
                    "messages": messages,
                    "context": {"campaign_context": "round-robin-test"},
                    "source": "playground",
                    "understander": "rule",
                    "decomposer": "deterministic",
                    "router": "database",
                    "aggregator": "deterministic",
                },
            )
        else:
            response = await env["client"].post(
                "/api/v1/runtime/requests",
                headers={**_auth(key), "Idempotency-Key": f"runtime-{body_marker[:100]}"},
                json={"prompt": text, "context": {"campaign_context": "round-robin-test"}},
            )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result.get("status") == "completed", result
        return result

    headers = {"Idempotency-Key": f"latency-{case}-{body_marker[:100]}"}
    messages = _message(size, body_marker)
    if case == "stream":
        stream = await client.chat.completions.create(
            model="auto", messages=messages, stream=True,
            stream_options={"include_usage": True}, temperature=0.2, extra_headers=headers,
        )
        chunks = [chunk async for chunk in stream]
        assert chunks and any(chunk.choices for chunk in chunks)
        return chunks
    if case == "tools":
        return await client.chat.completions.create(
            model="auto", messages=messages,
            tools=[{"type": "function", "function": {
                "name": "lookup", "description": "look up a value",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            }}],
            tool_choice="auto", extra_headers=headers,
        )
    if case == "schema":
        return await client.chat.completions.create(
            model="auto", messages=messages,
            response_format={"type": "json_schema", "json_schema": {
                "name": "answer", "strict": True,
                "schema": {"type": "object", "properties": {"answer": {"type": "string"}},
                           "required": ["answer"], "additionalProperties": False},
            }},
            extra_headers=headers,
        )
    completion = await client.chat.completions.create(
        model="auto", messages=messages, n=2 if case == "n2" else 1,
        **({"temperature": 0.2} if case == "chat" else {}),
        extra_headers=headers,
    )
    if case == "n2":
        assert len(completion.choices) == 2
    return completion


@pytest.mark.asyncio
@pytest.mark.latency_config_overrides(
    min_training_samples=120,
    min_new_samples=60,
    min_profile_train_samples=8,
    min_profile_validation_samples=2,
    min_common_validation_samples=2,
    min_profile_recent_successes=8,
    min_profile_recent_pairs=2,
    min_residual_samples=2,
    training_poll_seconds=1,
    training_interval_seconds=2,
    history_refresh_seconds=1,
    history_max_age_seconds=30,
    writer_batch_size=5,
    writer_flush_seconds=0.05,
    training_max_api_inflight=2,
)
async def test_auto_learning_campaign_trains_publishes_serves_and_retrains(latency_campaign_env, monkeypatch):
    env = latency_campaign_env
    assert env["runtime"].config.min_training_samples == 120
    assert env["runtime"].config.min_new_samples == 60
    assert env["runtime"].config.training_poll_seconds == 1
    assert env["runtime"].model_store.snapshot() is None
    env["set_upstream_delay"](
        lambda body: 35.0 + min(len(json.dumps(body, ensure_ascii=False)), 2200) * 0.08
    )
    # Real streaming makes this request a streaming latency profile; temperature
    # selects the SDK message-preserving execution path for plain/stream Chat.
    env["settings"].gateway_openai_real_streaming_enabled = True
    env["settings"].gateway_runtime_requests_per_minute = 600
    sdk_transport = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    from openai import AsyncOpenAI

    async with AsyncOpenAI(
        base_url="http://test/v1", api_key=env["key"], http_client=sdk_transport,
        max_retries=0,
    ) as sdk:
        overlap_evidence: dict[str, object] = {}
        create_subprocess = asyncio.create_subprocess_exec

        async def spawn_and_probe_api(*args, **kwargs):
            process = await create_subprocess(*args, **kwargs)
            command = [str(value) for value in args]
            if "src.backend_ai.latency.trainer" in command and not overlap_evidence:
                started = asyncio.get_running_loop().time()
                ticker_stop = asyncio.Event()
                ticker_ticks = 0

                async def ticker():
                    nonlocal ticker_ticks
                    while not ticker_stop.is_set():
                        ticker_ticks += 1
                        await asyncio.sleep(0.001)

                ticker_task = asyncio.create_task(ticker())
                try:
                    response = await sdk.models.list()
                finally:
                    ticker_stop.set()
                    await ticker_task
                overlap_evidence.update({
                    "child_pid": process.pid,
                    "response_completed_while_child_active": process.returncode is None,
                    "response_elapsed_ms": round((asyncio.get_running_loop().time() - started) * 1000, 3),
                    "model_count": len(response.data),
                    "event_loop_ticker_ticks": ticker_ticks,
                })
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_and_probe_api)

        # First round proves both public entry points start on the catalog and
        # that the low-sample scheduler does not spawn a child prematurely.
        for case in CASES:
            await _send_case(env, sdk, case, 80, surface="playground" if case == "native" else None)
        await wait_for_campaign(lambda: _row_count(env["factory"], 6))
        early_rows = await _rows(env["factory"])
        assert len(early_rows) == 6
        assert all(row.resolution_source == "catalog" for row in early_rows)
        assert all(row.resolution_reason == "no_artifact" for row in early_rows)
        await asyncio.sleep(1.2)
        assert await _runs(env["factory"]) == []

        # Round-robin creates 20 chronologically ordered, mock-measured rows
        # for each of the six profile shapes. The mock delay depends on actual
        # request-body size, so the worker receives measured labels rather than
        # rows synthesized directly in the database.
        for round_number in range(1, 20):
            for case in CASES:
                surface = ("playground" if round_number % 2 == 0 else "runtime") if case == "native" else None
                await _send_case(env, sdk, case, 80 + round_number * 70, surface=surface)
        await wait_for_campaign(lambda: _row_count(env["factory"], 120))
        all_rows = await _rows(env["factory"])
        assert len(all_rows) == 120
        profile_counts = Counter(_case_key(row) for row in all_rows)
        assert profile_counts == Counter({case: 20 for case in CASES}), [
            (row.source, row.features_json.get("protocol"), row.features_json.get("streaming"),
             row.features_json.get("has_tools"), row.features_json.get("has_schema"), row.features_json.get("n"))
            for row in all_rows
        ]
        assert all(row.status == "success" and row.provider_total_ms > 0 for row in all_rows)
        assert all(row.resolution_source == "catalog" and row.resolution_reason == "no_artifact" for row in all_rows)
        assert all(row.execution_mode == "live" and row.mode == "auto" for row in all_rows)
        assert not any("campaign-" in json.dumps(row.features_json) for row in all_rows)
        from src.backend_ai.latency.history import is_usable_latency_label
        from src.backend_ai.workers.latency_worker import should_train

        usable_count = sum(is_usable_latency_label(row) for row in all_rows)
        assert usable_count == 120, f"usable labels={usable_count}"
        assert should_train(
            datetime.now(UTC), None, usable_count, usable_count,
            env["runtime"].collector.inflight_count,
            env["runtime"].collector.queue_fraction,
            env["runtime"].config,
        ), {
            "usable": usable_count,
            "inflight": env["runtime"].collector.inflight_count,
            "queue_fraction": env["runtime"].collector.queue_fraction,
            "config": env["runtime"].config,
        }

        await wait_for_campaign(lambda: _has_finished_run(env["factory"], 1), timeout_seconds=45)
        first_run = (await _runs(env["factory"]))[0]
        assert first_run.status == "published", first_run.report_json
        assert first_run.error_code is None
        assert first_run.sample_count == 120
        assert first_run.dataset_digest and len(first_run.dataset_digest) == 64
        assert first_run.artifact_json is not None
        assert overlap_evidence.get("response_completed_while_child_active") is True, overlap_evidence
        assert overlap_evidence.get("response_elapsed_ms", 0) > 0
        assert overlap_evidence.get("event_loop_ticker_ticks", 0) > 0
        artifact = validate_artifact(first_run.artifact_json)
        assert artifact.version == first_run.candidate_version
        assert artifact.checksum == first_run.artifact_json["checksum"]
        report = artifact.validation_report
        assert report["accepted"] is True, report
        assert report["global_metrics"]["common_count"] >= 2
        assert set(report["profile_metrics"]) == {row.profile_key for row in all_rows}
        for metrics in report["profile_metrics"].values():
            assert metrics["train_count"] >= 8
            assert metrics["validation_count"] >= 2
            assert metrics["gate_pass"] is True, metrics
        await wait_for_campaign(lambda: _active_version(env["factory"], artifact.version))
        await wait_for_campaign(lambda: _history_version(env["runtime"], artifact.version))
        assert env["runtime"].model_store.snapshot().version == artifact.version

        # Independently pin the interval condition while the sample gate is met.
        finished_at = first_run.finished_at
        assert finished_at is not None
        assert not should_train(
            finished_at + timedelta(seconds=1), finished_at, 180, 60, 0, 0.0, env["runtime"].config
        )
        assert should_train(
            finished_at + timedelta(seconds=3), finished_at, 180, 60, 0, 0.0, env["runtime"].config
        )

        # Probe every trained shape, plus Playground and native runtime,
        # Responses sharing the SDK/plain profile, and one unseen combination.
        # These 9 requests are part of the 60 post-publish samples below.
        probe_begin = len(await _rows(env["factory"]))
        marker = "PINNED_FIRST_GENERATION_REQUEST"
        entered, release = env["arm_upstream_hold"](marker)
        held_task = asyncio.create_task(_send_case(env, sdk, "chat", 700, marker=marker))
        await asyncio.wait_for(entered.wait(), timeout=10)

        await _send_case(env, sdk, "native", 650, marker="playground-pdf-context-", surface="playground")
        await _send_case(env, sdk, "native", 680, marker="runtime-context-", surface="runtime")
        for case in ("stream", "tools", "schema", "n2"):
            await _send_case(env, sdk, case, 650, marker=f"inference-{case}-")
        response = await sdk.responses.create(
            model="auto", input="response inference profile " + "r" * 600,
            extra_headers={"Idempotency-Key": "latency-response-inference"},
        )
        assert response.output_text == "mock upstream response"
        unseen = await sdk.chat.completions.create(
            model="auto", messages=_message(650, "unseen-stream-tools-"), stream=True,
            tools=[{"type": "function", "function": {"name": "lookup", "parameters": {
                "type": "object", "properties": {"q": {"type": "string"}},
            }}}],
            extra_headers={"Idempotency-Key": "latency-unseen-profile"},
        )
        unseen_chunks = [chunk async for chunk in unseen]
        assert unseen_chunks
        for index in range(51):
            case = CASES[index % len(CASES)]
            await _send_case(env, sdk, case, 350 + (index % 12) * 65, marker=f"retrain-fill-{index}-")

        await wait_for_campaign(lambda: _row_count(env["factory"], probe_begin + 59))
        assert len(await _runs(env["factory"])) == 1
        release.set()
        await held_task
        await wait_for_campaign(lambda: _row_count(env["factory"], probe_begin + 60))
        probes = (await _rows(env["factory"]))[probe_begin:probe_begin + 9]
        known = probes[:-1]
        predicted_cases = {_case_key(row) for row in known if row.resolution_source == "predicted"}
        assert set(CASES) <= predicted_cases, [
            (row.features_json, row.resolution_source, row.resolution_reason) for row in probes
        ]
        assert all(row.prediction_version == artifact.version for row in known if row.resolution_source == "predicted")
        assert any(row.source == "openai_sdk" and row.features_json["protocol"] == "openai_sdk" and
                   row.resolution_source == "predicted" for row in probes)  # Responses SDK
        unseen_row = probes[-1]
        assert unseen_row.resolution_source == "catalog"
        assert unseen_row.resolution_reason == "unsupported_profile"
        ranking_evidence = _assert_live_artifact_changes_ranking(
            env,
            artifact,
            next(row for row in known if _case_key(row) == "chat"),
        )

        # The first post-publish request started before all of the other 59;
        # its immutable resolution retains the old version after retraining.
        pinned = (await _rows(env["factory"]))[probe_begin]
        assert pinned.resolution_source == "predicted"
        assert pinned.prediction_version == artifact.version

        await wait_for_campaign(lambda: _has_finished_run(env["factory"], 2), timeout_seconds=45)
        runs = await _runs(env["factory"])
        second_run = runs[1]
        assert second_run.status == "published", second_run.report_json
        assert second_run.sample_count >= 180
        assert second_run.candidate_version != first_run.candidate_version
        assert second_run.artifact_json and second_run.report_json["runtime_metrics"]["process_status"] == "completed"
        second_artifact = validate_artifact(second_run.artifact_json)
        await wait_for_campaign(lambda: _active_version(env["factory"], second_artifact.version))
        await wait_for_campaign(lambda: _history_version(env["runtime"], second_artifact.version))
        assert env["runtime"].model_store.generation == 2
        assert pinned.prediction_version == first_run.candidate_version

        # Stop and restart the AI backend lifespan against the same test DB.
        # The new process-local stores must load the persisted second generation
        # before serving another prediction.
        rows_before_restart = await _rows(env["factory"])
        stopped_runtime, restarted_runtime = await env["restart_backend_ai_lifespan"]()
        assert stopped_runtime.collector.queue.empty()
        await wait_for_campaign(lambda: _active_version(env["factory"], second_artifact.version))
        await wait_for_campaign(lambda: _history_version(restarted_runtime, second_artifact.version))
        assert restarted_runtime.model_store.snapshot() is not None
        assert restarted_runtime.model_store.snapshot().version == second_artifact.version
        await _send_case(env, sdk, "chat", 600, marker="restart-inference-")
        await wait_for_campaign(lambda: _row_count(env["factory"], len(rows_before_restart) + 1))
        restarted_row = (await _rows(env["factory"]))[-1]
        assert restarted_row.resolution_source == "predicted"
        assert restarted_row.prediction_version == second_artifact.version

        # All data is from real routed requests; no code mutates labels or
        # validation gates. The recorded mock law is increasing with input size.
        assert min(row.provider_total_ms for row in all_rows if row.features_json["input_proxy"] < 100) < max(
            row.provider_total_ms for row in all_rows if row.features_json["input_proxy"] > 300
        )
        final_rows = await _rows(env["factory"])
        print("LATENCY_CAMPAIGN_EVIDENCE " + json.dumps({
            "initial_samples": len(all_rows),
            "initial_profile_counts": dict(profile_counts),
            "first_run": {
                "status": first_run.status,
                "sample_count": first_run.sample_count,
                "version": first_run.candidate_version,
                "created_at": first_run.created_at.isoformat(),
                "published_at": first_run.finished_at.isoformat(),
                "runtime": first_run.report_json.get("runtime_metrics", {}),
                "validation": thaw(report),
            },
            "inference": {
                "predicted_observations": sum(row.resolution_source == "predicted" for row in final_rows),
                "catalog_fallbacks": dict(Counter(
                    row.resolution_reason or "unknown"
                    for row in final_rows if row.resolution_source == "catalog"
                )),
            },
            "second_run": {
                "status": second_run.status,
                "sample_count": second_run.sample_count,
                "version": second_run.candidate_version,
                "created_at": second_run.created_at.isoformat(),
                "published_at": second_run.finished_at.isoformat(),
                "runtime": second_run.report_json.get("runtime_metrics", {}),
                "validation": thaw(second_artifact.validation_report),
            },
            "post_publish_sample_count": 60,
            "api_request_during_ridge_child": overlap_evidence,
            "pinned_request_version": pinned.prediction_version,
            "active_generation": env["runtime"].model_store.generation,
            "ranking": ranking_evidence,
            "restart_inference_version": restarted_row.prediction_version,
            "database_observation_count": len(final_rows),
            "mock_latency_ms_min": round(min(row.provider_total_ms for row in all_rows), 3),
            "mock_latency_ms_max": round(max(row.provider_total_ms for row in all_rows), 3),
        }, sort_keys=True))


async def _row_count(factory, expected: int) -> bool:
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(LatencyObservationRecord))
    return int(count or 0) >= expected


async def _has_finished_run(factory, count: int) -> bool:
    async with factory() as session:
        rows = list((await session.scalars(select(LatencyTrainingRunRecord))).all())
    return len(rows) >= count and all(row.status != "running" for row in rows[:count])


async def _active_version(factory, version: str) -> bool:
    from src.shared.models.latency import LatencyModelState

    async with factory() as session:
        state = await session.get(LatencyModelState, "generation-v1")
    return state is not None and state.active_version == version


async def _history_version(runtime, version: str) -> bool:
    return runtime.history_cache.model_version == version
