import io
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.dtos.applications import ApplicationCreate
from src.backend.services.application_service import ApplicationService
from src.backend_ai.gateway.files.service import (
    FileLimits,
    FileNotFoundForApplicationError,
    FileServiceError,
    FileTooLargeError,
    GatewayFileService,
    StorageQuotaExceededError,
)
from src.backend_ai.gateway.files.storage import FileStorageError, LocalFileStorage, S3FileStorage
from src.shared.database.base import Base
from src.shared.models.audit import AuditEventRecord
from src.shared.models.core import utc_now
from src.shared.models.files import GatewayFileRecord
from tests.test_gateway.test_multimodal import PNG, _pdf


@pytest_asyncio.fixture
async def files_env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'files.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        apps = ApplicationService(session)
        app_a = await apps.create("user-1", ApplicationCreate(slug="app-a", name="A"))
        app_b = await apps.create("user-1", ApplicationCreate(slug="app-b", name="B"))
    storage = LocalFileStorage(tmp_path / "blobs")
    async with factory() as session:
        yield session, storage, app_a.id, app_b.id
    await engine.dispose()


def _service(session, storage, **limits):
    return GatewayFileService(session, storage, FileLimits(**limits))


@pytest.mark.asyncio
async def test_upload_defaults_to_one_day_and_round_trips(files_env):
    session, storage, app_a, _ = files_env
    service = _service(session, storage)
    pdf = _pdf(["Quarterly plan " * 20])
    record = await service.create(app_a, filename="plan.pdf", data=pdf, declared_mime_type="text/plain")

    assert record.id.startswith("file-") and record.mime_type == "application/pdf"  # bytes beat the claim
    assert record.expires_at - record.created_at == timedelta(days=1)
    assert record.storage_backend == "local"
    _, data = await service.content(app_a, record.id)
    assert data == pdf
    [resolved] = (await service.resolve(app_a, {record.id})).values()
    assert resolved.data == pdf and resolved.filename == "plan.pdf"
    actions = list(await session.scalars(select(AuditEventRecord.action)))
    assert "gateway_file.created" in actions


def _file_record(application_id: str, file_id: str, created_at: datetime, *, purpose: str = "vision"):
    return GatewayFileRecord(
        id=file_id,
        application_id=application_id,
        filename=f"{file_id}.txt",
        purpose=purpose,
        mime_type="text/plain",
        bytes=1,
        sha256="0" * 64,
        storage_backend="local",
        storage_key=f"{application_id}/{file_id}",
        created_at=created_at,
        expires_at=created_at + timedelta(days=1),
    )


@pytest.mark.asyncio
async def test_file_pages_tie_break_by_id_in_both_orders(files_env):
    session, storage, app_a, _ = files_env
    instant = utc_now()
    session.add_all([_file_record(app_a, f"file-{suffix}", instant) for suffix in "cba"])
    await session.commit()
    service = _service(session, storage)

    for order, expected in (("asc", ["file-a", "file-b", "file-c"]), ("desc", ["file-c", "file-b", "file-a"])):
        found = []
        cursor = None
        while True:
            page = await service.list_page(app_a, limit=1, after=cursor, order=order)
            found.extend(record.id for record in page.records)
            if not page.has_more:
                break
            cursor = page.records[-1].id
        assert found == expected


@pytest.mark.asyncio
async def test_file_page_filters_before_limit(files_env):
    session, storage, app_a, _ = files_env
    instant = utc_now()
    session.add_all(
        [
            _file_record(app_a, "file-a", instant, purpose="user_data"),
            _file_record(app_a, "file-b", instant, purpose="vision"),
            _file_record(app_a, "file-c", instant, purpose="user_data"),
            _file_record(app_a, "file-d", instant, purpose="vision"),
        ]
    )
    await session.commit()

    page = await _service(session, storage).list_page(app_a, limit=1, purpose="vision")
    assert [record.id for record in page.records] == ["file-d"]
    assert page.has_more is True
    next_page = await _service(session, storage).list_page(app_a, limit=1, after="file-d", purpose="vision")
    assert [record.id for record in next_page.records] == ["file-b"]
    assert next_page.has_more is False


@pytest.mark.parametrize("cursor_state", ["deleted", "expired"])
@pytest.mark.asyncio
async def test_file_cursor_remains_usable_after_delete_or_expiry(files_env, cursor_state):
    session, storage, app_a, _ = files_env
    instant = utc_now()
    session.add_all([_file_record(app_a, f"file-{suffix}", instant) for suffix in "abc"])
    await session.commit()
    service = _service(session, storage)
    first_page = await service.list_page(app_a, limit=1, order="asc")
    cursor_id = first_page.records[-1].id
    cursor_record = await session.get(GatewayFileRecord, cursor_id)
    if cursor_state == "deleted":
        cursor_record.deleted_at = instant
    else:
        cursor_record.expires_at = utc_now() - timedelta(seconds=1)
    await session.commit()

    next_page = await service.list_page(app_a, limit=1, after=cursor_id, order="asc")

    assert [record.id for record in next_page.records] == ["file-b"]
    assert next_page.has_more is True


@pytest.mark.asyncio
async def test_other_application_deleted_and_expired_files_are_all_not_found(files_env):
    session, storage, app_a, app_b = files_env
    service = _service(session, storage)
    record = await service.create(app_a, filename="a.png", data=PNG)

    with pytest.raises(FileNotFoundForApplicationError):
        await service.get(app_b, record.id)

    record.expires_at = utc_now() - timedelta(seconds=1)
    await session.commit()
    with pytest.raises(FileNotFoundForApplicationError):
        await service.get(app_a, record.id)
    assert await service.purge_expired() == 1
    assert not (storage.root / record.storage_key).exists()

    other = await service.create(app_a, filename="b.png", data=PNG)
    await service.delete(app_a, other.id)
    with pytest.raises(FileNotFoundForApplicationError):
        await service.resolve(app_a, {other.id})


@pytest.mark.asyncio
async def test_validation_limits_and_quota(files_env):
    session, storage, app_a, _ = files_env
    service = _service(session, storage, max_file_bytes=100, application_quota_bytes=120)

    with pytest.raises(FileServiceError, match="Unsupported file type"):
        await service.create(app_a, filename="x.zip", data=b"PK\x03\x04" + b"\x00" * 20)
    with pytest.raises(FileServiceError, match="UTF-8"):
        await service.create(app_a, filename="x.txt", data=b"\xff\xfe\xfa", declared_mime_type="text/plain")
    with pytest.raises(FileTooLargeError):
        await service.create(app_a, filename="big.png", data=PNG + b"\x00" * 200)
    with pytest.raises(FileServiceError, match="expires_after"):
        await service.create(app_a, filename="a.png", data=PNG, expires_after_seconds=5)

    await service.create(app_a, filename="a.png", data=PNG)  # 40 bytes
    await service.create(app_a, filename="notes.md", data=b"# notes\n" * 5, declared_mime_type="text/markdown")
    with pytest.raises(StorageQuotaExceededError):
        await service.create(app_a, filename="c.png", data=PNG + b"\x00" * 40)


@pytest.mark.asyncio
async def test_local_storage_rejects_keys_outside_root(tmp_path):
    storage = LocalFileStorage(tmp_path / "root")
    with pytest.raises(FileStorageError):
        await storage.put("../escape", b"x", content_type="text/plain")


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, ContentType):  # noqa: N803 - boto3 casing
        self.objects[(Bucket, Key)] = (Body, ContentType)

    def get_object(self, *, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][0])}

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.objects.pop((Bucket, Key), None)


@pytest.mark.asyncio
async def test_s3_storage_uses_bucket_prefix_and_maps_missing_objects(files_env):
    session, _, app_a, _ = files_env
    fake = FakeS3()
    storage = S3FileStorage("bucket", prefix="gateway-files", client=fake)
    service = _service(session, storage)
    record = await service.create(app_a, filename="a.png", data=PNG)

    assert ("bucket", f"gateway-files/{app_a}/{record.id}") in fake.objects
    assert fake.objects[("bucket", f"gateway-files/{app_a}/{record.id}")][1] == "image/png"
    assert (await session.get(GatewayFileRecord, record.id)).storage_backend == "s3"

    fake.objects.clear()  # bytes vanished from the bucket
    with pytest.raises(FileNotFoundForApplicationError):
        await service.content(app_a, record.id)
