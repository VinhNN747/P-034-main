"""OpenAI-compatible Files API: upload once, then reference the file by id in
chat requests instead of re-sending base64 every turn.

    f = client.files.create(file=open("plan.pdf", "rb"), purpose="user_data")
    {"type": "file", "file": {"file_id": f.id}}

Files belong to the calling application (not to a conversation) and expire
after ``GATEWAY_FILES_DEFAULT_TTL_SECONDS`` (1 day) unless ``expires_after``
asks otherwise.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.dependencies.openai_auth import get_openai_principal
from src.backend_ai.gateway.factory import build_file_limits, get_file_storage
from src.backend_ai.gateway.files.service import FileServiceError, GatewayFileService, as_utc
from src.backend_ai.gateway.files.storage import FileStorageError, FileStorageNotFound, FileStoragePermissionError
from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.shared.config import get_settings
from src.shared.database.session import get_db
from src.shared.models.files import GatewayFileRecord

router = APIRouter(prefix="/v1", tags=["openai-compatible-files"])
AppKey = Annotated[ApplicationPrincipal, Depends(get_openai_principal)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


class FileObject(BaseModel):
    id: str
    object: Literal["file"] = "file"
    bytes: int
    created_at: int
    expires_at: int
    filename: str
    purpose: str
    status: Literal["processed"] = "processed"
    status_details: str | None = None


class FileList(BaseModel):
    object: Literal["list"] = "list"
    data: list[FileObject]
    has_more: bool = False
    first_id: str | None = None
    last_id: str | None = None


class FileDeleted(BaseModel):
    id: str
    object: Literal["file"] = "file"
    deleted: bool = True


def _service(session: AsyncSession) -> GatewayFileService:
    settings = get_settings()
    return GatewayFileService(session, get_file_storage(settings), build_file_limits(settings),
        storage_resolver=lambda backend: get_file_storage(settings, backend=backend))


def _file_object(record: GatewayFileRecord) -> FileObject:
    return FileObject(
        id=record.id,
        bytes=record.bytes,
        created_at=int(as_utc(record.created_at).timestamp()),
        expires_at=int(as_utc(record.expires_at).timestamp()),
        filename=record.filename,
        purpose=record.purpose,
    )


def _openai_error(exc: FileServiceError) -> OpenAIAPIError:
    return OpenAIAPIError(exc.status_code, exc.message, code=exc.code, param=exc.param,
                          type='server_error' if exc.status_code >= 500 else 'invalid_request_error')


def _storage_error(exc):
    if isinstance(exc, FileStoragePermissionError):
        return OpenAIAPIError(500, 'File storage is not configured correctly.', type='server_error',
                              code='storage_configuration_error')
    return OpenAIAPIError(503, 'File storage is temporarily unavailable.', type='server_error', code='storage_unavailable')


@router.post("/files", response_model=FileObject)
async def upload_file(
    app: AppKey,
    session: DbSession,
    file: Annotated[UploadFile, File()],
    purpose: Annotated[str, Form()] = "user_data",
    expires_after_anchor: Annotated[str | None, Form(alias="expires_after[anchor]")] = None,
    expires_after_seconds: Annotated[int | None, Form(alias="expires_after[seconds]")] = None,
) -> FileObject:
    if expires_after_anchor not in (None, "created_at"):
        raise OpenAIAPIError(400, "expires_after[anchor] must be 'created_at'.", param="expires_after")
    service = _service(session)
    # Read one byte past the limit so oversize uploads fail without
    # buffering arbitrarily large bodies.
    data = await file.read(service.limits.max_file_bytes + 1)
    try:
        record = await service.create(
            app.application_id,
            filename=file.filename or "upload",
            data=data,
            purpose=purpose,
            declared_mime_type=file.content_type,
            expires_after_seconds=expires_after_seconds,
        )
    except FileServiceError as exc:
        raise _openai_error(exc) from exc
    except FileStorageError as exc:
        raise _storage_error(exc) from exc
    return _file_object(record)


@router.get("/files", response_model=FileList)
async def list_files(
    app: AppKey,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=10000)] = 10000,
    after: str | None = None,
    order: Literal["asc", "desc"] = "desc",
    purpose: str | None = None,
) -> FileList:
    try:
        page = await _service(session).list_page(
            app.application_id, limit=limit, after=after, order=order, purpose=purpose
        )
    except FileServiceError as exc:
        raise _openai_error(exc) from exc
    data = [_file_object(record) for record in page.records]
    return FileList(
        data=data,
        has_more=page.has_more,
        first_id=data[0].id if data else None,
        last_id=data[-1].id if data else None,
    )


@router.get("/files/{file_id}", response_model=FileObject)
async def retrieve_file(file_id: str, app: AppKey, session: DbSession) -> FileObject:
    try:
        return _file_object(await _service(session).get(app.application_id, file_id))
    except FileServiceError as exc:
        raise _openai_error(exc) from exc


@router.get("/files/{file_id}/content")
async def file_content(file_id: str, app: AppKey, session: DbSession) -> StreamingResponse:
    service = _service(session)
    try:
        record = await service.get(app.application_id, file_id)
        chunks = service.storage_for(record.storage_backend).iter_content(record.storage_key)
        first = await anext(chunks, b'')  # storage errors before SSE/HTTP headers
    except FileServiceError as exc:
        raise _openai_error(exc) from exc
    except FileStorageNotFound as exc:
        raise OpenAIAPIError(404, 'Stored file was not found.', param='file_id', code='file_not_found') from exc
    except FileStorageError as exc:
        raise _storage_error(exc) from exc
    async def content():
        try:
            if first:
                yield first
            async for chunk in chunks:
                yield chunk
        finally:
            await chunks.aclose()
    return StreamingResponse(content(), media_type=record.mime_type,
        headers={'Content-Length': str(record.bytes)})


@router.delete("/files/{file_id}", response_model=FileDeleted)
async def delete_file(file_id: str, app: AppKey, session: DbSession) -> FileDeleted:
    try:
        record = await _service(session).delete(app.application_id, file_id)
    except FileServiceError as exc:
        raise _openai_error(exc) from exc
    return FileDeleted(id=record.id)
