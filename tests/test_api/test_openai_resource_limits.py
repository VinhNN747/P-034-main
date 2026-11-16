from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request

from src.backend_ai.services.openai_request_context import install_openai_request_context
from tests.test_api.test_openai_compat import openai_env as openai_env


@pytest.mark.asyncio
@pytest.mark.parametrize('chunked', [False, True])
async def test_multipart_body_limit_prevents_parser_reading_an_unbounded_body(monkeypatch, chunked):
    from src.shared import config
    monkeypatch.setattr(config, 'get_settings', lambda: SimpleNamespace(
        gateway_file_multipart_max_body_bytes=10, gateway_openai_max_body_bytes=32,
        gateway_embeddings_max_body_bytes=8))
    application = FastAPI()
    install_openai_request_context(application)
    @application.post('/v1/files')
    async def upload(request: Request):
        return {'size': len(await request.body())}
    async def body():
        for _ in range(4):
            yield b'12345'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://test') as client:
        response = await client.post('/v1/files', content=body() if chunked else b'12345678901')
    assert response.status_code == 413
    assert response.json()['error']['code'] == 'request_too_large'
    assert response.headers['x-request-id']


@pytest.mark.asyncio
async def test_download_uses_storage_chunks_instead_of_full_blob_get(openai_env, monkeypatch):
    from src.backend_ai.gateway.factory import get_file_storage
    storage = get_file_storage(openai_env['settings'])
    headers = {'Authorization': 'Bearer ' + openai_env['key']}
    uploaded = await openai_env['client'].post('/v1/files', headers=headers,
        files={'file': ('chunk.txt', b'content', 'text/plain')}, data={'purpose': 'user_data'})
    async def full_read(_key):
        raise AssertionError('download must not allocate the full blob')
    monkeypatch.setattr(storage, 'get', full_read)
    response = await openai_env['client'].get('/v1/files/' + uploaded.json()['id'] + '/content', headers=headers)
    assert response.status_code == 200
    assert response.content == b'content'
