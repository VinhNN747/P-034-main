import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from openai import AsyncOpenAI, BadRequestError
from sqlalchemy import func, select

from src.backend_ai.gateway.files.service import FileLimits, GatewayFileService
from src.backend_ai.gateway.files.storage import FileStorageError
from src.backend_ai.gateway.model_alias import resolve_model
from src.backend_ai.routers.openai_compat import _stream, _to_chat_completion
from src.shared.dtos.gateway import OutputEvidence
from src.shared.dtos.runtime_api import RuntimeRequestResponse
from src.shared.models.files import GatewayFileRecord
from src.shared.models.runtime import RuntimeOperationRecord
from tests.service_app import app
from tests.test_api.test_openai_compat import openai_env as openai_env


class Ambiguous503Transport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.ASGITransport(app=app)
        self.injected = False
        self.attempts = 0
        self.completed_ids = []

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if request.url.path == "/v1/chat/completions":
            self.attempts += 1
            if response.status_code == 200:
                self.completed_ids.append(json.loads(await response.aread())["id"])
                if not self.injected:
                    self.injected = True
                    return httpx.Response(
                        503,
                        json={
                            "error": {
                                "message": "Injected ambiguous response",
                                "type": "server_error",
                                "code": "proxy_unavailable",
                                "param": None,
                            }
                        },
                        headers={"retry-after-ms": "1"},
                        request=request,
                    )
        return response


@pytest.mark.asyncio
@pytest.mark.parametrize("stable_key", [True, False])
async def test_default_sdk_retry_does_not_duplicate_execution(openai_env, stable_key):
    openai_env["settings"].gateway_openai_idempotency_policy = "strict"
    transport = Ambiguous503Transport()
    async with AsyncOpenAI(
        base_url="http://test/v1",
        api_key=openai_env["key"],
        http_client=httpx.AsyncClient(transport=transport),
        timeout=10,
    ) as client:
        if not stable_key:
            with pytest.raises(BadRequestError) as error:
                await client.chat.completions.create(model="auto", messages=[{"role": "user", "content": "Hello"}])
            assert error.value.code == "missing_idempotency_key"
            async with openai_env["factory"]() as session:
                assert await session.scalar(select(func.count()).select_from(RuntimeOperationRecord)) == 0
            return
        result = await client.chat.completions.create(
            model="auto",
            messages=[{"role": "user", "content": "Hello"}],
            extra_headers={"Idempotency-Key": "sdk-stable-key"},
        )
    async with openai_env["factory"]() as session:
        count = await session.scalar(select(func.count()).select_from(RuntimeOperationRecord))
    print(
        json.dumps(
            {
                "probe": "default_retry",
                "stable_key": stable_key,
                "http_attempts": transport.attempts,
                "operation_count": count,
                "unique_success_ids": len(set(transport.completed_ids)),
            }
        )
    )
    assert transport.attempts == 2
    assert count == 1, "One logical SDK call produced multiple executions after an ambiguous 503"
    assert result.id == transport.completed_ids[0]


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_chat_preserves_measured_finish_reason(reason):
    receipt = RuntimeRequestResponse(
        request_id="probe",
        application_id="app",
        status="completed",
        execution_mode="live",
        output="partial output",
        config_version=1,
        started_at=datetime.now(UTC),
        output_evidence=OutputEvidence(finish_reason=reason),
    )
    completion = _to_chat_completion(receipt, resolve_model("auto"), ["controlled-model"])
    assert completion.choices[0].finish_reason == reason


@pytest.mark.asyncio
async def test_chat_sse_preserves_length():
    receipt = RuntimeRequestResponse(
        request_id="probe",
        application_id="app",
        status="completed",
        execution_mode="live",
        output="partial output",
        config_version=1,
        started_at=datetime.now(UTC),
        output_evidence=OutputEvidence(finish_reason="length"),
    )
    completion = _to_chat_completion(receipt, resolve_model("auto"), ["controlled-model"])
    events = [json.loads(event[6:]) async for event in _stream(completion, True) if event.startswith("data: {")]
    reasons = [choice["finish_reason"] for event in events for choice in event["choices"] if choice["finish_reason"]]
    assert reasons == ["length"]


@pytest.mark.asyncio
async def test_file_storage_outage_is_server_error(openai_env, monkeypatch):
    from src.backend_ai.gateway.factory import get_file_storage
    from src.backend_ai.routers import openai_files

    async with AsyncOpenAI(
        base_url="http://test/v1",
        api_key=openai_env["key"],
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False)),
    ) as client:
        uploaded = await client.files.create(file=("probe.txt", b"audit", "text/plain"), purpose="user_data")
        storage = get_file_storage(openai_env["settings"])

        async def unavailable(key):
            raise FileStorageError("Injected storage outage")

        monkeypatch.setattr(storage, "get", unavailable)
        async def unavailable_chunks(key):
            raise FileStorageError("Injected storage outage")
            yield b""
        monkeypatch.setattr(storage, "iter_content", unavailable_chunks)
        monkeypatch.setattr(openai_files, "get_file_storage", lambda settings: storage)
        raw = await openai_env["client"].get(
            "/v1/files/" + uploaded.id + "/content", headers={"Authorization": "Bearer " + openai_env["key"]}
        )
    print(
        json.dumps(
            {"probe": "storage_outage", "http_status": raw.status_code, "code": raw.json().get("error", {}).get("code")}
        )
    )
    assert raw.status_code >= 500, "Storage outage was classified as a missing file"


@pytest.mark.asyncio
async def test_file_quota_is_atomic_for_concurrent_uploads(openai_env):
    async with openai_env["factory"]() as session:
        from src.shared.models.core import Application

        application_id = await session.scalar(select(Application.id).where(Application.slug == "openai-sdk"))

    class BarrierStorage:
        name = "local"

        def __init__(self):
            self.count = 0
            self.ready = asyncio.Event()

        async def put(self, key, data, *, content_type):
            self.count += 1
            await asyncio.sleep(0.1)

        async def delete(self, key):
            pass

        async def get(self, key):
            return b"1234"

    storage = BarrierStorage()

    async def upload(index):
        async with openai_env["factory"]() as session:
            service = GatewayFileService(session, storage, FileLimits(application_quota_bytes=5))
            return await service.create(
                application_id, filename=f"{index}.txt", data=b"1234", declared_mime_type="text/plain"
            )

    results = await asyncio.gather(upload(1), upload(2), return_exceptions=True)
    async with openai_env["factory"]() as session:
        used = await session.scalar(select(func.coalesce(func.sum(GatewayFileRecord.bytes), 0)))
    print(
        json.dumps(
            {
                "probe": "quota_race",
                "quota_bytes": 5,
                "used_bytes": used,
                "successes": sum(not isinstance(r, BaseException) for r in results),
                "errors": [type(r).__name__ for r in results if isinstance(r, BaseException)],
            },
            default=str,
        )
    )
    assert used == 4, "Concurrent uploads must reserve before writing to storage"
    assert sum(not isinstance(r, BaseException) for r in results) == 1


@pytest.mark.asyncio
async def test_file_upload_storage_failure_has_openai_error_envelope(openai_env, monkeypatch):
    from src.backend_ai.routers import openai_files

    class BrokenStorage:
        name = "local"

        async def put(self, key, data, *, content_type):
            raise FileStorageError("Injected write failure")

    monkeypatch.setattr(openai_files, "get_file_storage", lambda settings: BrokenStorage())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as raw:
        r = await raw.post(
            "/v1/files",
            headers={"Authorization": "Bearer " + openai_env["key"]},
            files={"file": ("probe.txt", b"audit", "text/plain")},
            data={"purpose": "user_data"},
        )
    envelope = False
    try:
        envelope = isinstance(r.json().get("error"), dict)
    except ValueError:
        pass
    print(
        json.dumps(
            {
                "probe": "storage_write_failure",
                "http_status": r.status_code,
                "openai_envelope": envelope,
                "request_id_present": bool(r.headers.get("x-request-id")),
            }
        )
    )
    assert r.status_code >= 500
    assert envelope
    assert r.headers.get("x-request-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("GET", "/v1/unknown"), ("PUT", "/v1/models")])
async def test_routing_errors_use_openai_envelope_and_request_id(openai_env, method, path):
    response = await openai_env["client"].request(method, path)
    assert response.status_code in (404, 405)
    assert isinstance(response.json().get("error"), dict)
    assert response.headers.get("x-request-id")


@pytest.mark.asyncio
async def test_tool_choice_required_without_tools_rejected_before_execution(openai_env):
    response = await openai_env["client"].post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer " + openai_env["key"], "Idempotency-Key": "invalid-tool-choice"},
        json={"model": "auto", "messages": [{"role": "user", "content": "hello"}], "tool_choice": "required"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "tool_choice"
    async with openai_env["factory"]() as session:
        assert await session.scalar(select(func.count()).select_from(RuntimeOperationRecord)) == 0


@pytest.mark.asyncio
async def test_delete_is_durable_even_when_blob_cleanup_fails(openai_env, monkeypatch):
    from src.backend_ai.gateway.factory import get_file_storage
    from src.backend_ai.routers import openai_files

    storage = get_file_storage(openai_env["settings"])
    response = await openai_env["client"].post(
        "/v1/files",
        headers={"Authorization": "Bearer " + openai_env["key"]},
        files={"file": ("outbox.txt", b"content", "text/plain")},
        data={"purpose": "user_data"},
    )
    file_id = response.json()["id"]

    async def unavailable(_key):
        raise FileStorageError("private storage error")

    monkeypatch.setattr(storage, "delete", unavailable)
    monkeypatch.setattr(openai_files, "get_file_storage", lambda _: storage)
    deleted = await openai_env["client"].delete(
        "/v1/files/" + file_id, headers={"Authorization": "Bearer " + openai_env["key"]}
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    unavailable_file = await openai_env["client"].get(
        "/v1/files/" + file_id, headers={"Authorization": "Bearer " + openai_env["key"]}
    )
    assert unavailable_file.status_code == 404
    from src.shared.models.files import FileDeletionOutbox

    async with openai_env["factory"]() as session:
        assert await session.get(FileDeletionOutbox, file_id) is not None


@pytest.mark.asyncio
async def test_hard_cap_reaches_public_execution_options(openai_env, monkeypatch):
    from src.backend_ai.gateway.providers.offline import OfflineProvider
    observed = []
    original = OfflineProvider.generate
    async def capture(self, prompt, **kwargs):
        observed.append(kwargs.get('options'))
        return await original(self, prompt, **kwargs)
    monkeypatch.setattr(OfflineProvider, 'generate', capture)
    response = await openai_env['client'].post('/v1/chat/completions',
        headers={'Authorization': 'Bearer ' + openai_env['key'], 'Idempotency-Key': 'hard-cap-execution'},
        json={'model': 'auto', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_completion_tokens': 48})
    assert response.status_code == 200
    assert observed[0].hard_completion_token_limit == 48


@pytest.mark.asyncio
async def test_same_chat_key_changing_stream_mode_conflicts(openai_env):
    headers = {'Authorization': 'Bearer ' + openai_env['key'], 'Idempotency-Key': 'stream-conflict'}
    body = {'model': 'auto', 'messages': [{'role': 'user', 'content': 'hello'}]}
    first = await openai_env['client'].post('/v1/chat/completions', json=body, headers=headers)
    assert first.status_code == 200
    second = await openai_env['client'].post('/v1/chat/completions', json={**body, 'stream': True}, headers=headers)
    assert second.status_code == 409
    assert second.json()['error']['code'] == 'idempotency_conflict'


@pytest.mark.asyncio
async def test_reasoning_exhausted_cap_returns_length_without_fallback(openai_env, monkeypatch):
    from src.backend_ai.gateway.providers.base import ProviderResponse
    from src.backend_ai.gateway.providers.offline import OfflineProvider
    attempts = []
    async def truncated(self, prompt, **kwargs):
        attempts.append(kwargs['model_id'])
        return ProviderResponse(text='', provider='offline', model_id=kwargs['model_id'],
            input_tokens=5, output_tokens=48, usage_received=True,
            output_evidence=OutputEvidence(finish_reason='length', reasoning_tokens=48))
    monkeypatch.setattr(OfflineProvider, 'generate', truncated)
    response = await openai_env['client'].post('/v1/chat/completions',
        headers={'Authorization': 'Bearer ' + openai_env['key'], 'Idempotency-Key': 'empty-length'},
        json={'model': 'auto', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_completion_tokens': 48})
    assert response.status_code == 200, response.json()
    assert response.json()['choices'][0]['finish_reason'] == 'length'
    assert len(attempts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['upload','download'])
@pytest.mark.parametrize('kind,status,code', [
    ('outage',503,'storage_unavailable'), ('permission',500,'storage_configuration_error')])
async def test_storage_taxonomy_is_consistent_before_headers(openai_env, monkeypatch, action, kind, status, code):
    from src.backend_ai.gateway.files.storage import FileStoragePermissionError, FileStorageUnavailable
    from src.backend_ai.routers import openai_files
    storage = openai_files.get_file_storage(openai_env['settings'])
    monkeypatch.setattr(openai_files, 'get_file_storage', lambda settings, backend=None: storage)
    client = openai_env['client']
    headers = {'Authorization':'Bearer '+openai_env['key']}
    error_type = FileStoragePermissionError if kind == 'permission' else FileStorageUnavailable
    if action == 'upload':
        async def failed_put(*args, **kwargs):
            raise error_type('private-bucket-path')
        monkeypatch.setattr(storage,'put',failed_put)
        result = await client.post('/v1/files',headers=headers,
            files={'file':('a.txt',b'ok','text/plain')},data={'purpose':'user_data'})
    else:
        file = await client.post('/v1/files',headers=headers,
            files={'file':('a.txt',b'ok','text/plain')},data={'purpose':'user_data'})
        async def failed_chunks(*args, **kwargs):
            raise error_type('private-bucket-path')
            yield
        monkeypatch.setattr(storage,'iter_content',failed_chunks)
        result = await client.get('/v1/files/'+file.json()['id']+'/content',headers=headers)
    assert result.status_code == status and result.json()['error']['code'] == code
    assert 'private-bucket-path' not in result.text and result.headers['x-request-id']


@pytest.mark.asyncio
async def test_historical_chat_replays_but_new_sampling_conflicts(openai_env, monkeypatch):
    from sqlalchemy import select

    from src.backend_ai.dtos.openai_compat import ChatCompletionRequest
    from src.backend_ai.gateway.model_alias import resolve_model
    from src.backend_ai.gateway.providers.offline import OfflineProvider
    from src.backend_ai.routers.openai_compat import _to_runtime_payload
    from src.backend_ai.services.runtime_operation_service import RuntimeOperationService
    from src.shared.models.runtime import RuntimeOperationRecord
    client = openai_env['client']
    body = {'model': 'auto:fast', 'messages': [{'role': 'user', 'content': 'Hello'}]}
    headers = {'Authorization': 'Bearer ' + openai_env['key'], 'Idempotency-Key': 'historical-chat'}
    first = await client.post('/v1/chat/completions', json=body, headers=headers)
    assert first.status_code == 200
    payload = _to_runtime_payload(ChatCompletionRequest.model_validate(body), resolve_model(body['model']))
    payload.metadata.pop('openai_hard_completion_token_limit', None)
    async with openai_env['factory']() as session:
        operation = await session.scalar(select(RuntimeOperationRecord))
        operation.normalization_version = 1
        operation.payload_hash = RuntimeOperationService.fingerprint(payload)
        await session.commit()
    async def unexpected(*args, **kwargs):
        raise AssertionError('A historical replay must not call the provider')
    monkeypatch.setattr(OfflineProvider, 'generate', unexpected)
    replay = await client.post('/v1/chat/completions', json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    changed = await client.post('/v1/chat/completions', json={**body, 'temperature': 0}, headers=headers)
    assert changed.status_code == 409 and changed.json()['error']['code'] == 'idempotency_conflict'
