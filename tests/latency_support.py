from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.shared.models  # noqa: F401
from src.backend.dtos.application_keys import ApplicationKeyCreate
from src.backend.dtos.applications import ApplicationCreate
from src.backend.dtos.catalog import ModelCreate, ModelPoolCreate, ModelPoolMemberCreate, PolicyCreate, ProviderCreate
from src.backend.dtos.gateway_configuration import GatewayConfigUpsert
from src.backend.services.application_key_service import ApplicationKeyService
from src.backend.services.application_service import ApplicationService
from src.backend.services.catalog_service import CatalogService
from src.backend.services.gateway_configuration_service import GatewayConfigurationService
from src.backend_ai.dependencies import openai_auth
from src.backend_ai.latency.contracts import LatencyConfig
from src.backend_ai.routers import openai_compat, openai_embeddings, openai_files, openai_responses, runtime
from src.backend_ai.services import embedding_execution_service, openai_model_service
from src.backend_ai.workers import lifespan
from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser
from src.shared.models.latency import LatencyModelState, LatencyObservationRecord
from src.shared.models.provider_credentials import ProviderCredential
from src.shared.services.credential_crypto import CredentialCrypto
from tests.service_app import ai_app, app


async def wait_for_campaign(
    predicate: Callable[[], Awaitable[bool]], *, timeout_seconds: float = 30.0
) -> None:
    """Poll an eventual DB/runtime condition with a bounded, diagnostic timeout."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"latency campaign condition was not reached within {timeout_seconds:.1f}s")


@pytest_asyncio.fixture
async def latency_campaign_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request):
    """Live-mode application with a real credential resolver and an in-process upstream stub."""
    from src.backend_ai.routers import gateway_runtime

    config_marker = request.node.get_closest_marker("latency_config_overrides")
    if config_marker is not None:
        original_from_settings = LatencyConfig.from_settings.__func__
        overrides = dict(config_marker.kwargs)

        def test_config_from_settings(cls, source_settings):
            return replace(original_from_settings(cls, source_settings), **overrides)

        monkeypatch.setattr(LatencyConfig, "from_settings", classmethod(test_config_from_settings))

    settings = Settings(
        _env_file=None,
        database_url=os.getenv(
            "LATENCY_TEST_POSTGRES_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'latency-campaign.db'}",
        ),
        gateway_execution_mode="live",
        gateway_runtime_enabled=True,
        gateway_openai_enabled=True,
        gateway_openai_idempotency_policy="best_effort",
        gateway_latency_mode="auto",
        gateway_latency_work_dir=str(tmp_path / "trainer-work"),
        gateway_files_local_dir=str(tmp_path / "file-blobs"),
        gateway_files_storage="local",
        gateway_latency_history_refresh_seconds=1,
        gateway_latency_training_poll_seconds=1,
        gateway_latency_training_interval_seconds=60,
        gateway_file_maintenance_enabled=False,
        comparison_worker_enabled=False,
        gateway_openai_real_streaming_enabled=False,
        gateway_ocr_enabled=False,
        gateway_runtime_deadline_seconds=60,
        gateway_runtime_requests_per_minute=120,
        gateway_runtime_max_concurrent_requests=8,
        gateway_decomposition_mode="legacy",
        credential_encryption_key=Fernet.generate_key().decode("ascii"),
    )
    schema = "latency_campaign_" + uuid4().hex
    isolated_url = os.getenv("LATENCY_TEST_POSTGRES_URL")
    admin_engine = create_async_engine(isolated_url) if isolated_url else None
    if isolated_url:
        if not isolated_url.startswith("postgresql+psycopg://"):
            raise AssertionError("LATENCY_TEST_POSTGRES_URL must be an explicit async psycopg test URL")
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            isolated_url,
            connect_args={"options": f"-csearch_path={schema}"},
            pool_size=6,
            max_overflow=0,
        )
    else:
        engine = create_async_engine(settings.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        session.add(LatencyModelState(scope="generation-v1"))
        owner = "latency-test-owner"
        application = await ApplicationService(session).create(
            owner, ApplicationCreate(slug="latency-test", name="Latency test")
        )
        other_application = await ApplicationService(session).create(
            owner, ApplicationCreate(slug="latency-test-other", name="Latency test other")
        )
        catalog = CatalogService(session)
        provider = await catalog.create_provider(ProviderCreate(
            name="mock-openai", display_name="Mock OpenAI", kind="openai",
        ))
        model = await catalog.create_model(ModelCreate(
            provider_id=provider.id,
            name="mock-chat-model",
            display_name="Mock chat model",
            status="active",
            capabilities=["chat", "text", "structured_output", "long_context"],
            context_window=16_000,
            target_latency_ms=900,
        ))
        embedding_model = await catalog.create_model(ModelCreate(
            provider_id=provider.id,
            name="text-embedding-3-small",
            display_name="Mock embedding model",
            status="active",
            capabilities=["embedding"],
            input_price_per_million=0.02,
            pricing_version="test",
        ))
        for app_record, suffix in ((application, "a"), (other_application, "b")):
            pool = await catalog.create_pool(ModelPoolCreate(
                name=f"latency-pool-{suffix}",
                purpose="runtime",
                members=[
                    ModelPoolMemberCreate(model_id=model.id),
                    ModelPoolMemberCreate(model_id=embedding_model.id),
                ],
            ), owner)
            policy = await catalog.create_policy(PolicyCreate(
                name=f"latency-policy-{suffix}", fallback_chain=[model.id],
            ))
            await GatewayConfigurationService(session).upsert(
                owner,
                app_record.id,
                GatewayConfigUpsert(
                    model_pool_id=pool.id,
                    routing_policy_id=policy.id,
                    default_understander="rule",
                    default_decomposer="deterministic",
                    default_router="database",
                    default_aggregator="deterministic",
                    max_output_tokens=32,
                    timeout_seconds=60,
                    retry_limit=0,
                    fallback_enabled=False,
                    budget_ceiling=0.05,
                ),
            )
        credential_id = str(uuid4())
        crypto = CredentialCrypto.from_settings(settings)
        assert crypto is not None
        session.add(ProviderCredential(
            id=credential_id,
            provider_id=provider.id,
            encrypted_api_key=crypto.encrypt(
                provider_id=provider.id,
                credential_id=credential_id,
                version=1,
                api_key="latency-campaign-upstream-key",
            ),
            version=1,
            status="active",
        ))
        application_key = await ApplicationKeyService(session).create(
            owner, application.id, ApplicationKeyCreate(name="latency campaign")
        )

    original_overrides = dict(app.dependency_overrides)

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(
            id=owner,
            email="latency-test@example.invalid",
            roles=("owner",),
            permissions=frozenset(),
        )

    app.dependency_overrides[get_db] = override_db
    from src.shared.dependencies.auth import get_current_user
    app.dependency_overrides[get_current_user] = override_user

    for module in (
        gateway_runtime,
        runtime,
        openai_compat,
        openai_embeddings,
        openai_files,
        openai_responses,
        openai_auth,
        embedding_execution_service,
        openai_model_service,
    ):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(lifespan, "get_settings", lambda: settings)
    monkeypatch.setattr(lifespan, "create_session_factory", lambda: factory)

    calls: list[dict] = []

    def default_delay_policy(_body: dict) -> float:
        return 20.0

    delay_policy: Callable[[dict], float] = default_delay_policy
    upstream_holds: dict[str, tuple[asyncio.Event, asyncio.Event]] = {}

    def set_upstream_delay(policy: Callable[[dict], float]) -> None:
        nonlocal delay_policy
        delay_policy = policy

    def arm_upstream_hold(marker: str) -> tuple[asyncio.Event, asyncio.Event]:
        entered = asyncio.Event()
        release = asyncio.Event()
        upstream_holds[marker] = (entered, release)
        return entered, release

    def response_for(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        calls.append({
            "path": request.url.path,
            "body": body,
            "model": body.get("model"),
            "prompt": body.get("prompt"),
        })
        if request.url.path.endswith("/embeddings"):
            inputs = body.get("input", "")
            count = len(inputs) if isinstance(inputs, list) and inputs and isinstance(inputs[0], str) else 1
            dimensions = body.get("dimensions") or 1536
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": body.get("model", "text-embedding-3-small"),
                    "data": [
                        {"object": "embedding", "index": index, "embedding": [0.125] * dimensions}
                        for index in range(count)
                    ],
                    "usage": {"prompt_tokens": count, "total_tokens": count},
                },
                request=request,
            )
        response_text = "mock upstream response"
        response_format = body.get("response_format") or {}
        if response_format.get("type") == "json_schema":
            response_text = '{"answer":"ok"}'
        n = body.get("n", 1)
        n = n if isinstance(n, int) and 1 <= n <= 4 else 1
        if body.get("stream") is True:
            created = 1_797_000_000
            chunks = []
            for index in range(n):
                chunks.append({
                    "id": "chatcmpl-latency-campaign",
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": body.get("model", "mock-chat-model"),
                    "choices": [{"index": index, "delta": {"content": "mock "}, "finish_reason": None}],
                })
                chunks.append({
                    "id": "chatcmpl-latency-campaign",
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": body.get("model", "mock-chat-model"),
                    "choices": [{"index": index, "delta": {"content": "upstream response"}, "finish_reason": "stop"}],
                })
            if (body.get("stream_options") or {}).get("include_usage"):
                chunks.append({
                    "id": "chatcmpl-latency-campaign",
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": body.get("model", "mock-chat-model"),
                    "choices": [],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
                })
            encoded = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            return httpx.Response(
                200,
                content=encoded,
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-latency-campaign",
                "object": "chat.completion",
                "created": 1_797_000_000,
                "model": body.get("model", "mock-chat-model"),
                "choices": [
                    {
                        "index": index,
                        "message": {"role": "assistant", "content": response_text},
                        "finish_reason": "stop",
                    }
                    for index in range(n)
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            },
            request=request,
        )

    original_async_send = httpx.AsyncClient.send
    original_sync_send = httpx.Client.send

    async def guarded_async_send(self, request, *args, **kwargs):
        if request.url.host != "test":
            body = json.loads(request.content or b"{}")
            serialized = json.dumps(body, ensure_ascii=False)
            for marker, (entered, release) in tuple(upstream_holds.items()):
                if marker in serialized and not entered.is_set():
                    entered.set()
                    await release.wait()
                    break
            await asyncio.sleep(max(0.0, delay_policy(body)) / 1000)
            return response_for(request)
        return await original_async_send(self, request, *args, **kwargs)

    def guarded_sync_send(self, request, *args, **kwargs):
        if request.url.host != "test":
            return response_for(request)
        return original_sync_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_async_send)
    monkeypatch.setattr(httpx.Client, "send", guarded_sync_send)

    manager = lifespan.comparison_lifespan(ai_app)
    manager_entered = False
    await manager.__aenter__()
    manager_entered = True
    env = {
        "factory": factory,
        "settings": settings,
        "runtime": ai_app.state.latency_runtime,
        "calls": calls,
        "set_upstream_delay": set_upstream_delay,
        "arm_upstream_hold": arm_upstream_hold,
        "key": application_key.raw_key,
        "application_id": application.id,
        "other_application_id": other_application.id,
        "model_id": model.id,
        "provider_id": provider.id,
        "owner_id": owner,
        "tmp_path": tmp_path,
    }

    async def restart_backend_ai_lifespan():
        nonlocal manager, manager_entered
        stopped_runtime = env["runtime"]
        await manager.__aexit__(None, None, None)
        manager_entered = False
        assert not hasattr(ai_app.state, "latency_runtime")
        manager = lifespan.comparison_lifespan(ai_app)
        await manager.__aenter__()
        manager_entered = True
        env["runtime"] = ai_app.state.latency_runtime
        return stopped_runtime, env["runtime"]

    env["restart_backend_ai_lifespan"] = restart_backend_ai_lifespan

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            env["client"] = client
            yield env
        if manager_entered:
            await manager.__aexit__(None, None, None)
            manager_entered = False
        assert not hasattr(ai_app.state, "latency_runtime")
        assert env["runtime"].collector.queue.empty()
    finally:
        if manager_entered:
            await manager.__aexit__(None, None, None)
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
        await engine.dispose()
        if admin_engine is not None:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin_engine.dispose()


async def count_latency_observations(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(LatencyObservationRecord)) or 0)
