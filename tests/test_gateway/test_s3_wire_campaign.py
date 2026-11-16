"""Real boto3 HTTP against a disposable S3 emulator, plus HTTP faults."""
import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config

from src.backend_ai.gateway.files.service import GatewayFileService
from src.backend_ai.gateway.files.storage import (
    FileStorageNotFound,
    FileStoragePermissionError,
    FileStorageUnavailable,
    S3FileStorage,
)
from tests.test_gateway.test_files import files_env as files_env


def client(endpoint):
    return boto3.client('s3', endpoint_url=endpoint, region_name='us-east-1',
        aws_access_key_id='fixture', aws_secret_access_key='fixture',
        config=Config(connect_timeout=0.3, read_timeout=0.3, retries={'max_attempts':0},
                      s3={'addressing_style':'path'}))


@pytest.mark.asyncio
async def test_s3_http_roundtrip_chunking_missing_object_and_cleanup(files_env):
    endpoint = os.getenv('OPENAI_AUDIT_S3_EMULATOR_URL')
    if endpoint is None:
        pytest.skip('Explicit isolated S3 emulator URL required')
    assert urlsplit(endpoint).hostname in {'127.0.0.1', 'localhost'}
    session, _, app_id, other_id = files_env
    sdk = client(endpoint)
    bucket = 'sdk-audit-' + uuid4().hex
    await asyncio.to_thread(sdk.create_bucket, Bucket=bucket)
    storage = S3FileStorage(bucket, client=sdk)
    service = GatewayFileService(session, storage)
    try:
        data = b'a' * (8*1024*1024)
        record = await service.create(app_id, filename='large.txt', data=data, declared_mime_type='text/plain')
        chunks = [chunk async for chunk in storage.iter_content(record.storage_key)]
        assert len(chunks) == 128 and all(len(chunk) == 65536 for chunk in chunks)
        assert b''.join(chunks) == data
        from src.backend_ai.gateway.files.service import FileNotFoundForApplicationError
        with pytest.raises(FileNotFoundForApplicationError):
            await service.content(other_id, record.id)
        await service.delete(app_id, record.id)
        assert record.lifecycle == 'deleted'
        with pytest.raises(FileStorageNotFound):
            await storage.get(record.storage_key)
        assert not (await asyncio.to_thread(sdk.list_objects_v2, Bucket=bucket)).get('Contents')
    finally:
        objects = (await asyncio.to_thread(sdk.list_objects_v2, Bucket=bucket)).get('Contents', [])
        for item in objects:
            await asyncio.to_thread(sdk.delete_object, Bucket=bucket, Key=item['Key'])
        await asyncio.to_thread(sdk.delete_bucket, Bucket=bucket)
        sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code,expected', [
    (403,'AccessDenied',FileStoragePermissionError),
    (503,'ServiceUnavailable',FileStorageUnavailable),
])
async def test_s3_http_faults_are_classified_and_sanitized(status, code, expected):
    class Fault(BaseHTTPRequestHandler):
        def do_GET(self):
            body = f'<Error><Code>{code}</Code><Message>private-bucket-url</Message></Error>'.encode()
            self.send_response(status)
            self.send_header('Content-Type','application/xml')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1',0), Fault)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sdk = client(f'http://127.0.0.1:{server.server_port}')
    storage = S3FileStorage('fixture', client=sdk)
    try:
        with pytest.raises(expected) as error:
            await storage.get('app/file')
        assert 'private-bucket-url' not in str(error.value)
    finally:
        sdk.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
