"""Self-hosted OpenAI-compatible servers (vLLM, Ollama) behind ``local`` connections."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from src.backend.services import pool_rules
from src.backend.services.provider_connection_service import (
    ProviderConnectionInvalidError,
    ProviderConnectionService,
)
from src.backend_ai.gateway.providers.base import GenerationOptions
from src.backend_ai.gateway.providers.local import LocalProvider
from src.backend_ai.gateway.providers.offline import OfflineProvider
from src.backend_ai.gateway.providers.openai_messages import request_options
from src.backend_ai.services.provider_client_builder import ProviderClientBuilder
from src.shared.config import Settings
from src.shared.services.local_endpoints import LocalEndpointError, is_local_endpoint, normalize_local_base_url


def _settings(mode: str) -> Settings:
    return Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", gateway_execution_mode=mode)


def _provider(*, base_url: str | None, kind: str = "local"):
    return SimpleNamespace(id="p-local", name="my-vllm", kind=kind, purpose="target", status="active", base_url=base_url)


class _OpenAICompatibleServer(BaseHTTPRequestHandler):
    """Answers /v1/chat/completions like vLLM and Ollama do, and records each request body."""

    requests: list[dict] = []

    def do_POST(self):  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "body": body})
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in (
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "xin "}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"content": "chào"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}},
            ):
                payload = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": body["model"], **chunk}
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        payload = {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "xin chào"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    _OpenAICompatibleServer.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAICompatibleServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", _OpenAICompatibleServer.requests
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:11434", "http://localhost:11434/v1"),
        ("http://localhost:8000/v1/", "http://localhost:8000/v1"),
        (" https://gpu-box.lan:8000/openai/v1 ", "https://gpu-box.lan:8000/openai/v1"),
    ],
)
def test_local_base_url_is_normalized(raw, expected):
    assert normalize_local_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw", ["localhost:8000", "ftp://localhost/v1", "http:///v1", "http://u:p@localhost/v1", "http://h/v1?x=1"]
)
def test_unusable_local_base_url_is_rejected(raw):
    with pytest.raises(LocalEndpointError):
        normalize_local_base_url(raw)


def test_connection_service_accepts_a_local_base_url_only_for_local():
    assert ProviderConnectionService._canonical_base_url("local", "http://localhost:11434") == "http://localhost:11434/v1"
    assert ProviderConnectionService._canonical_base_url("local", None) is None
    with pytest.raises(ProviderConnectionInvalidError):
        ProviderConnectionService._canonical_base_url("local", "localhost:8000")
    with pytest.raises(ProviderConnectionInvalidError):
        ProviderConnectionService._canonical_base_url("google", "http://localhost:8000/v1")


@pytest.mark.parametrize("mode", ["offline", "live"])
@pytest.mark.asyncio
async def test_builder_makes_a_real_client_for_a_local_server_in_both_modes(mode):
    builder = ProviderClientBuilder(resolver=None, settings=_settings(mode))

    client = await builder.build_for_provider(_provider(base_url="http://localhost:8000/v1"))

    assert isinstance(client, LocalProvider)


@pytest.mark.asyncio
async def test_local_connection_without_a_base_url_stays_the_offline_stub():
    stub = _provider(base_url=None)

    assert isinstance(await ProviderClientBuilder(None, _settings("offline")).build_for_provider(stub), OfflineProvider)
    assert await ProviderClientBuilder(None, _settings("live")).build_for_provider(stub) is None
    assert not is_local_endpoint(stub)


def test_local_servers_are_callable_in_live_mode_without_a_credential():
    settings = _settings("live")
    kinds = {"p-local": "local", "p-stub": "local"}

    callable_ids = pool_rules.callable_provider_ids(settings, {"p-local", "p-stub"}, kinds, {"p-local"})

    assert callable_ids == {"p-local"}


def test_local_requests_use_the_classic_max_tokens_field():
    options = GenerationOptions(messages=[{"role": "user", "content": "hi"}], max_output_tokens=64)

    assert request_options("qwen2.5:1.5b", options, max_tokens_field=True)["max_tokens"] == 64
    assert "max_completion_tokens" not in request_options("qwen2.5:1.5b", options, max_tokens_field=True)
    assert "max_completion_tokens" in request_options("gpt-4o-mini", options)


@pytest.mark.asyncio
async def test_local_provider_generates_against_an_openai_compatible_server(local_server):
    base_url, requests = local_server
    provider = LocalProvider(base_url=base_url)

    response = await provider.generate(
        "chào", task_id="t1", model_id="o3-mini-local", timeout_seconds=5, options=GenerationOptions(max_output_tokens=32)
    )

    assert response.text == "xin chào"
    assert (response.input_tokens, response.output_tokens, response.provider) == (7, 2, "local")
    [request] = requests
    assert request["path"] == "/v1/chat/completions"
    assert request["body"]["model"] == "o3-mini-local"
    # A model name that looks like an OpenAI reasoning model still gets no OpenAI-only fields.
    assert request["body"]["max_tokens"] == 32
    assert "max_completion_tokens" not in request["body"]
    assert "reasoning_effort" not in request["body"]


@pytest.mark.asyncio
async def test_local_provider_streams_and_reports_usage(local_server):
    base_url, requests = local_server
    provider = LocalProvider(base_url=base_url)

    events = [
        event
        async for event in provider.stream(
            "chào", task_id="t1", model_id="Qwen/Qwen2.5-1.5B-Instruct", timeout_seconds=5,
            options=GenerationOptions(max_output_tokens=16),
        )
    ]

    final = events[-1].final
    assert final.text == "xin chào"
    assert (final.input_tokens, final.output_tokens, final.usage_received) == (7, 2, True)
    assert requests[0]["body"]["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_local_provider_message_protocol(local_server):
    base_url, requests = local_server
    provider = LocalProvider(base_url=base_url)
    options = GenerationOptions(messages=[{"role": "user", "content": "chào"}], max_output_tokens=16)

    response = await provider.generate("", task_id="t1", model_id="qwen2.5:1.5b", timeout_seconds=5, options=options)

    assert response.text == "xin chào"
    assert requests[0]["body"]["max_tokens"] == 16
    assert "max_completion_tokens" not in requests[0]["body"]


@pytest.mark.asyncio
async def test_model_probe_calls_the_local_server_even_in_offline_mode(local_server, tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.backend_ai.services.model_probe_execution_service import ModelProbeExecutionService
    from src.shared.database.base import Base
    from src.shared.models.gateway import Model, Provider

    base_url, requests = local_server
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'probe.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(Provider(id="p-local", name="my-ollama", display_name="Ollama", kind="local", purpose="target",
                             status="active", base_url=base_url))
        session.add(Model(id="m-local", provider_id="p-local", name="qwen2.5:1.5b", display_name="Qwen 1.5B",
                          status="inactive", capabilities_json=["chat"]))
        await session.commit()

        result = await ModelProbeExecutionService(session, _settings("offline")).probe("m-local")
    await engine.dispose()

    assert (result.status, result.failure_reason) == ("healthy", None)
    assert requests[0]["body"]["model"] == "qwen2.5:1.5b"
