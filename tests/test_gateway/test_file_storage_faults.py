import asyncio
from pathlib import Path

import pytest

from src.backend_ai.gateway.files.storage import (
    FileStoragePermissionError,
    FileStorageUnavailable,
    LocalFileStorage,
    S3FileStorage,
)


@pytest.mark.asyncio
async def test_local_permission_is_classified_for_read_write_delete(tmp_path, monkeypatch):
    storage = LocalFileStorage(tmp_path)
    def fail(*args, **kwargs):
        raise PermissionError('private-path')
    monkeypatch.setattr(Path, 'read_bytes', fail)
    with pytest.raises(FileStoragePermissionError):
        await storage.get('app/file')
    monkeypatch.setattr(Path, 'mkdir', fail)
    with pytest.raises(FileStoragePermissionError):
        await storage.put('app/file', b'hello', content_type='text/plain')
    monkeypatch.setattr(Path, 'unlink', fail)
    with pytest.raises(FileStoragePermissionError):
        await storage.delete('app/file')


@pytest.mark.asyncio
async def test_s3_put_and_delete_outages_are_classified_without_sensitive_error():
    class Client:
        def put_object(self, **kwargs):
            raise ConnectionError('private-url')
        def delete_object(self, **kwargs):
            raise ConnectionError('private-url')
    storage = S3FileStorage('fixture', client=Client())
    for action in (storage.put('app/file', b'hello', content_type='text/plain'), storage.delete('app/file')):
        with pytest.raises(FileStorageUnavailable) as error:
            await action
        assert 'private-url' not in str(error.value)


@pytest.mark.asyncio
async def test_local_cancelled_write_finishes_before_compensation_can_delete(tmp_path, monkeypatch):
    import threading
    entered, finish = threading.Event(), threading.Event()
    original = Path.write_bytes
    def write(self, data):
        entered.set()
        finish.wait(timeout=3)
        return original(self, data)
    monkeypatch.setattr(Path, 'write_bytes', write)
    storage = LocalFileStorage(tmp_path)
    task = asyncio.create_task(storage.put('app/file', b'hello', content_type='text/plain'))
    await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done(), 'Compensation must wait for the thread, otherwise the blob can reappear'
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await storage.delete('app/file')
    assert not (tmp_path / 'app/file').exists()
    assert not (tmp_path / 'app/file.part').exists()


@pytest.mark.asyncio
async def test_cancelled_download_open_closes_late_created_handle(tmp_path, monkeypatch):
    import io
    import threading
    entered, release = threading.Event(), threading.Event()
    handle = io.BytesIO(b'hello')
    def opening(*args, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return handle
    monkeypatch.setattr(Path,'open',opening)
    chunks = LocalFileStorage(tmp_path).iter_content('app/file')
    task = asyncio.create_task(anext(chunks))
    await asyncio.to_thread(entered.wait,1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handle.closed
