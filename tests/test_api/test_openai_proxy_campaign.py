"""Isolated, deterministic Nginx campaign; never uses a paid provider.

Run with OPENAI_AUDIT_PROXY=1 after installing nginx:stable-alpine.
"""
import asyncio
import hashlib
import json
import os
import resource
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from openai import AsyncOpenAI

from tests.test_api.test_openai_compat import openai_env as openai_env
from tests.test_api.test_openai_streaming_tcp import streaming_gateway as streaming_gateway

pytestmark = pytest.mark.skipif(os.getenv('OPENAI_AUDIT_PROXY') != '1',
                              reason='Explicit isolated Docker/Nginx campaign required')


@pytest_asyncio.fixture
async def nginx_gateway(streaming_gateway, tmp_path):
    env = streaming_gateway
    env['settings'].gateway_runtime_requests_per_minute = 2000
    backend_port = env['url'].split(':')[2].split('/')[0]
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        proxy_port = probe.getsockname()[1]
    source = Path('src/deploy/nginx.conf').read_text()
    config = source.replace('backend:8000', '127.0.0.1:' + backend_port).replace(
        'backend-ai:8001', '127.0.0.1:' + backend_port).replace('listen 8080;', f'listen {proxy_port};')
    config_path = tmp_path / 'nginx.conf'
    config_path.write_text(config)
    name = 'p034-sdk-proxy-' + uuid4().hex[:12]
    command = ['docker', 'run', '--rm', '-d', '--name', name, '--network', 'host',
               '-v', str(config_path) + ':/etc/nginx/nginx.conf:ro', 'nginx:stable-alpine']
    def run(args):
        return subprocess.run(args, check=True, capture_output=True, text=True)
    await asyncio.to_thread(run, command)
    url = f'http://127.0.0.1:{proxy_port}'
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    response = await client.get(url + '/v1/models', headers={'Authorization': 'Bearer ' + env['key']})
                    if response.status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise AssertionError('Audit Nginx did not start')
        yield {**env, 'url': url + '/v1', 'port': proxy_port,
               'nginx_config_sha256': hashlib.sha256(source.encode()).hexdigest()}
    finally:
        await asyncio.to_thread(run, ['docker', 'stop', name])


def percentiles(values):
    values = sorted(values)
    return {str(p): round(values[min(len(values)-1, int((len(values)-1)*p/100))], 2) for p in (50,95,99)}


@pytest.mark.asyncio
async def test_proxy_deterministic_load_limits_and_streaming(nginx_gateway):
    env = nginx_gateway
    report = {'evidence_kind': 'isolated-postgresql-nginx-deterministic-provider' if os.getenv('OPENAI_AUDIT_CONTRACT_PG_URL')
              else 'isolated-sqlite-nginx-deterministic-provider',
              'provider_first_delta_ms': 100, 'provider_completion_ms': 1000,
              'nginx_config_sha256': env['nginx_config_sha256'], 'batches': []}
    async with AsyncOpenAI(base_url=env['url'], api_key=env['key'], max_retries=0) as client:
        for concurrency in (1,4,8):
            semaphore = asyncio.Semaphore(concurrency)
            async def request(index):
                async with semaphore:
                    start = time.monotonic()
                    first = None
                    text = ''
                    stream = await client.chat.completions.create(model='auto:fast',
                        messages=[{'role':'user','content':'Say hello'}], stream=True,
                        extra_headers={'Idempotency-Key':f'proxy-{concurrency}-{index}'})
                    async for chunk in stream:
                        if chunk.choices and chunk.choices[0].delta.content:
                            first = first if first is not None else (time.monotonic()-start)*1000
                            text += chunk.choices[0].delta.content
                    elapsed = (time.monotonic()-start)*1000
                    assert text == ' hello world ' and first is not None
                    assert first < elapsed
                    return first, elapsed
            values = await asyncio.gather(*(request(index) for index in range(100)), return_exceptions=True)
            errors = sum(isinstance(value, BaseException) for value in values)
            success = [value for value in values if not isinstance(value, BaseException)]
            batch = {'concurrency':concurrency,'requests':100,'errors':errors,
                     'ttft_ms':percentiles([value[0] for value in success]) if success else {},
                     'completion_ms':percentiles([value[1] for value in success]) if success else {},
                     'rss_max_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
            report['batches'].append(batch)
            print(json.dumps(batch), flush=True)
            output = os.getenv('OPENAI_AUDIT_PROXY_REPORT')
            if output:
                Path(output).write_text(json.dumps(report, indent=2)+'\n')
            assert errors == 0, [type(value).__name__ for value in values if isinstance(value, BaseException)]
            assert max(value[0] for value in success) < 500

        # A held provider owns the only configured slot; overload has an
        # explicit 429 envelope rather than a dropped or extra execution.
        env['settings'].gateway_runtime_max_concurrent_requests = 1
        held = await client.chat.completions.create(model='auto:fast', messages=[{'role':'user','content':'Hello'}],
            stream=True, extra_headers={'Idempotency-Key':'proxy-held'})
        async with httpx.AsyncClient() as raw:
            rejected = await raw.post(env['url']+'/chat/completions', headers={
                'Authorization':'Bearer '+env['key'],'Idempotency-Key':'proxy-overload'},
                json={'model':'auto:fast','messages':[{'role':'user','content':'Hello'}]})
        assert rejected.status_code == 429 and rejected.json()['error']['type'] == 'rate_limit_error'
        await held.close()
        report['overload_429'] = True

    report['status'] = 'pass'
    output = os.getenv('OPENAI_AUDIT_PROXY_REPORT')
    if output:
        Path(output).write_text(json.dumps(report, indent=2)+'\n')


@pytest.mark.asyncio
@pytest.mark.parametrize('path,size', [('/v1/embeddings', 9000000),
    ('/v1/chat/completions', 34000000), ('/v1/files', 55000000), ('/v1/files/', 55000000)])
async def test_proxy_rejects_oversized_body_before_reading_it(nginx_gateway, path, size):
    env = nginx_gateway
    # Nginx rejects an oversized body from headers before allocating it.
    reader, writer = await asyncio.open_connection('127.0.0.1', env['port'])
    writer.write(f'POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {size}\r\nConnection: close\r\n\r\n'.encode())
    await writer.drain()
    try:
        headers = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
        length = next(int(line.split(b':',1)[1]) for line in headers.split(b'\r\n')
                      if line.lower().startswith(b'content-length:'))
        wire = headers + await asyncio.wait_for(reader.readexactly(length), 3)
    finally:
        writer.close()
        await writer.wait_closed()
    assert b' 413 ' in wire and b'"request_too_large"' in wire and b'x-request-id:' in wire.lower()


@pytest.mark.asyncio
async def test_proxy_slow_reader_abort_cancels_producer_and_releases_slot(nginx_gateway, monkeypatch):
    from src.backend_ai.gateway.providers.base import ProviderStreamEvent
    from src.backend_ai.gateway.providers.offline import OfflineProvider
    env = nginx_gateway
    env['settings'].gateway_runtime_max_concurrent_requests = 1
    cancelled = asyncio.Event()
    produced = []
    async def flood(self, *args, **kwargs):
        try:
            for index in range(1000):
                produced.append(index)
                yield ProviderStreamEvent(type='text_delta', text='x' * 65536)
        finally:
            cancelled.set()
    monkeypatch.setattr(OfflineProvider, 'stream', flood)
    body = json.dumps({'model':'auto:fast','messages':[{'role':'user','content':'Hello'}], 'stream':True}).encode()
    reader, writer = await asyncio.open_connection('127.0.0.1', env['port'])
    writer.write((f'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n'
                  f'Authorization: Bearer {env["key"]}\r\nIdempotency-Key: proxy-slow\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body)
    await writer.drain()
    try:
        response_headers = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
        assert b' 200 ' in response_headers
        # Headers only: the client stops consuming the response body.
        await asyncio.sleep(0.3)
        assert 0 < len(produced) < 1000, 'A bounded pipeline must backpressure a slow TCP client'
    finally:
        writer.close()
        await writer.wait_closed()
    await asyncio.wait_for(cancelled.wait(), 10)
    async with httpx.AsyncClient() as client:
        deadline = time.monotonic() + 8
        while True:
            replay = await client.post(env['url'] + '/chat/completions', headers={
                'Authorization':'Bearer '+env['key'], 'Idempotency-Key':'proxy-slow'}, json=json.loads(body))
            if replay.json().get('error', {}).get('code') == 'request_outcome_unknown':
                break
            assert time.monotonic() < deadline, 'Abort finalization must release the owner within its deadline'
            await asyncio.sleep(0.01)
        replay = await client.post(env['url'] + '/chat/completions', headers={
            'Authorization':'Bearer '+env['key'], 'Idempotency-Key':'proxy-slow'}, json=json.loads(body))
        assert replay.status_code == 409
        next_request = await client.post(env['url'] + '/chat/completions', headers={
            'Authorization':'Bearer '+env['key'], 'Idempotency-Key':'proxy-after-slow'},
            json={'model':'auto:fast','messages':[{'role':'user','content':'Hello'}]})
        assert next_request.status_code == 200, next_request.text
