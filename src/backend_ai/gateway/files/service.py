"""Files API business rules: what may be stored, by whom, for how long.

The gateway stays stateless towards conversations: files belong to an
application, and callers reference them by ``file_id`` in each request
that needs them. Every lookup is scoped to the caller's application, and
another application's file, a deleted file and an expired file are all
reported the same way ("not found") so ids leak nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.gateway.files.storage import (
    FileStorage,
    FileStorageError,
    FileStorageNotFound,
    FileStoragePermissionError,
)
from src.backend_ai.gateway.multimodal.attachments import (
    OCR_IMAGE_MIME_TYPES,
    TEXT_MIME_TYPES,
    ResolvedFile,
    sniff_mime_type,
)
from src.backend_ai.services.file_quota_service import FileQuotaExceededError, FileQuotaService
from src.shared.models.core import utc_now
from src.shared.models.files import FileDeletionOutbox, GatewayFileRecord
from src.shared.repositories.audit import AuditRepository

# Only types the attachment analyzer can actually use; accepting others
# would store files the gateway silently ignores.
ALLOWED_MIME_TYPES = frozenset({"application/pdf", *OCR_IMAGE_MIME_TYPES, *TEXT_MIME_TYPES})
ALLOWED_PURPOSES = frozenset({"user_data", "vision", "assistants"})


class FileServiceError(Exception):
    status_code = 400
    code = "invalid_file"

    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.param = param


class FileNotFoundForApplicationError(FileServiceError):
    status_code = 404
    code = "file_not_found"


class FileStorageUnavailableError(FileServiceError):
    status_code = 503
    code = "storage_unavailable"


class FileStorageConfigurationError(FileServiceError):
    status_code = 500
    code = "storage_configuration_error"


class FileTooLargeError(FileServiceError):
    status_code = 413
    code = "file_too_large"


class StorageQuotaExceededError(FileServiceError):
    status_code = 413
    code = "storage_quota_exceeded"


class FileInvalidCursorError(FileServiceError):
    code = "invalid_cursor"


@dataclass(frozen=True)
class FileLimits:
    max_file_bytes: int = 50 * 1024 * 1024
    application_quota_bytes: int = 1024 * 1024 * 1024
    default_ttl_seconds: int = 24 * 60 * 60
    min_ttl_seconds: int = 60 * 60
    max_ttl_seconds: int = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class FilePage:
    records: list[GatewayFileRecord]
    has_more: bool


def as_utc(value: datetime) -> datetime:
    # SQLite drops tzinfo on read; everything is stored in UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=utc_now().tzinfo)


class GatewayFileService:
    def __init__(self, session: AsyncSession, storage: FileStorage, limits: FileLimits | None = None, *, storage_resolver=None) -> None:
        self.session = session
        self.storage = storage
        self.limits = limits or FileLimits()
        self.audit = AuditRepository(session)
        self.storage_resolver = storage_resolver

    def storage_for(self, backend):
        if backend == self.storage.name:
            return self.storage
        if self.storage_resolver is not None:
            return self.storage_resolver(backend)
        raise FileStorageUnavailableError("The file's storage backend is not configured.")

    # -- create ---------------------------------------------------------------

    async def create(
        self,
        application_id: str,
        *,
        filename: str,
        data: bytes,
        purpose: str = "user_data",
        declared_mime_type: str | None = None,
        expires_after_seconds: int | None = None,
    ) -> GatewayFileRecord:
        if purpose not in ALLOWED_PURPOSES:
            raise FileServiceError(f"purpose must be one of {sorted(ALLOWED_PURPOSES)}", param="purpose")
        if not data:
            raise FileServiceError("The uploaded file is empty.", param="file")
        if len(data) > self.limits.max_file_bytes:
            raise FileTooLargeError(f"File exceeds the {self.limits.max_file_bytes} byte limit.", param="file")
        mime_type = self._mime_type(data, filename, declared_mime_type)
        ttl = self._ttl(expires_after_seconds)

        quota = FileQuotaService(self.session, self.limits.application_quota_bytes)
        reservation_id = f"file-{uuid4().hex[:24]}"
        try:
            await quota.reserve(application_id, len(data), reservation_id, storage_backend=self.storage.name)
        except FileQuotaExceededError as exc:
            raise StorageQuotaExceededError(str(exc)) from exc

        file_id = reservation_id
        storage_key = f"{application_id}/{file_id}"
        now = utc_now()
        record = GatewayFileRecord(
            id=file_id,
            application_id=application_id,
            filename=(filename or "upload")[:255],
            purpose=purpose,
            mime_type=mime_type,
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            storage_backend=self.storage.name,
            storage_key=storage_key,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )
        try:
            stop_lease = asyncio.Event()
            async def heartbeat():
                while not stop_lease.is_set():
                    try:
                        await asyncio.wait_for(stop_lease.wait(), timeout=30)
                    except TimeoutError:
                        if not await quota.renew(reservation_id):
                            return
            lease_task = asyncio.create_task(heartbeat())
            try:
                await self.storage.put(storage_key, data, content_type=mime_type)
            finally:
                stop_lease.set()
                import anyio
                with anyio.CancelScope(shield=True):
                    await lease_task
            self.session.add(record)
            await self.audit.add_event(
                actor_user_id=None,
                action="gateway_file.created",
                resource_type="gateway_file",
                resource_id=file_id,
                metadata={"application_id": application_id, "bytes": len(data), "mime_type": mime_type},
            )
            await quota.commit(reservation_id, file_id)
        except BaseException:
            await self.session.rollback()
            # A lost commit acknowledgement must not delete a committed blob.
            try:
                committed = await self.session.get(GatewayFileRecord, file_id)
            except Exception:
                raise
            if committed is not None:
                return committed
            try:
                await self.storage.delete(storage_key)
            except Exception:
                # Leave reservation charged for maintenance to recover the blob.
                pass
            else:
                await quota.release(reservation_id)
            raise
        return record

    def _mime_type(self, data: bytes, filename: str, declared: str | None) -> str:
        # Bytes win over what the caller claims; text has no magic number,
        # so for text the declared type / extension decides, after a UTF-8 check.
        mime_type = sniff_mime_type(data)
        if mime_type is None:
            guessed = (declared or "").split(";")[0].strip().lower()
            if guessed in {"", "application/octet-stream"}:
                guessed = mimetypes.guess_type(filename or "")[0]
            if guessed in TEXT_MIME_TYPES:
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise FileServiceError("Text files must be UTF-8 encoded.", param="file") from exc
                mime_type = guessed
        if mime_type not in ALLOWED_MIME_TYPES:
            raise FileServiceError(
                f"Unsupported file type '{mime_type or 'unknown'}'. Supported: {sorted(ALLOWED_MIME_TYPES)}",
                param="file",
            )
        return mime_type

    def _ttl(self, requested: int | None) -> int:
        if requested is None:
            return self.limits.default_ttl_seconds
        if not self.limits.min_ttl_seconds <= requested <= self.limits.max_ttl_seconds:
            raise FileServiceError(
                f"expires_after seconds must be between {self.limits.min_ttl_seconds} and "
                f"{self.limits.max_ttl_seconds}.",
                param="expires_after",
            )
        return requested

    # -- read -----------------------------------------------------------------

    async def get(self, application_id: str, file_id: str) -> GatewayFileRecord:
        record = await self.session.get(GatewayFileRecord, file_id)
        if (
            record is None
            or record.application_id != application_id
            or record.deleted_at is not None
            or as_utc(record.expires_at) <= utc_now()
        ):
            raise FileNotFoundForApplicationError(f"No such File object: {file_id}", param="file_id")
        return record

    async def list(self, application_id: str, *, limit: int = 100) -> list[GatewayFileRecord]:
        rows = await self.session.scalars(
            select(GatewayFileRecord)
            .where(
                GatewayFileRecord.application_id == application_id,
                GatewayFileRecord.deleted_at.is_(None),
                GatewayFileRecord.expires_at > utc_now(),
            )
            .order_by(GatewayFileRecord.created_at.desc())
            .limit(limit)
        )
        return list(rows)

    async def list_page(
        self,
        application_id: str,
        *,
        limit: int = 10000,
        after: str | None = None,
        order: Literal["asc", "desc"] = "desc",
        purpose: str | None = None,
    ) -> FilePage:
        if not 1 <= limit <= 10000:
            raise FileServiceError("limit must be between 1 and 10000.", param="limit")
        if order not in ("asc", "desc"):
            raise FileServiceError("order must be 'asc' or 'desc'.", param="order")

        now = utc_now()
        conditions = [
            GatewayFileRecord.application_id == application_id,
            GatewayFileRecord.deleted_at.is_(None),
            GatewayFileRecord.expires_at > now,
        ]
        if purpose is not None:
            conditions.append(GatewayFileRecord.purpose == purpose)

        if after is not None:
            # Resolve cursor metadata within the application, but deliberately
            # allow a soft-deleted or expired row to remain a paging position.
            cursor = await self.session.scalar(
                select(GatewayFileRecord).where(
                    GatewayFileRecord.id == after,
                    GatewayFileRecord.application_id == application_id,
                )
            )
            if cursor is None:
                raise FileInvalidCursorError("Invalid file cursor.", param="after")
            cursor_created_at = as_utc(cursor.created_at)
            if order == "asc":
                conditions.append(
                    or_(
                        GatewayFileRecord.created_at > cursor_created_at,
                        (GatewayFileRecord.created_at == cursor_created_at) & (GatewayFileRecord.id > cursor.id),
                    )
                )
                ordering = (GatewayFileRecord.created_at.asc(), GatewayFileRecord.id.asc())
            else:
                conditions.append(
                    or_(
                        GatewayFileRecord.created_at < cursor_created_at,
                        (GatewayFileRecord.created_at == cursor_created_at) & (GatewayFileRecord.id < cursor.id),
                    )
                )
                ordering = (GatewayFileRecord.created_at.desc(), GatewayFileRecord.id.desc())
        elif order == "asc":
            ordering = (GatewayFileRecord.created_at.asc(), GatewayFileRecord.id.asc())
        else:
            ordering = (GatewayFileRecord.created_at.desc(), GatewayFileRecord.id.desc())

        rows = list(
            await self.session.scalars(
                select(GatewayFileRecord)
                .where(*conditions)
                .order_by(*ordering)
                .limit(limit + 1)
            )
        )
        return FilePage(records=rows[:limit], has_more=len(rows) > limit)

    async def content(self, application_id: str, file_id: str) -> tuple[GatewayFileRecord, bytes]:
        record = await self.get(application_id, file_id)
        try:
            return record, await self.storage_for(record.storage_backend).get(record.storage_key)
        except FileStorageNotFound as exc:
            raise FileNotFoundForApplicationError(f"No such File object: {file_id}", param="file_id") from exc
        except FileStoragePermissionError as exc:
            raise FileStorageConfigurationError('File storage is not configured correctly.') from exc
        except FileStorageError as exc:
            raise FileStorageUnavailableError("File storage is temporarily unavailable.") from exc

    async def resolve(self, application_id: str, file_ids: set[str], *, max_analysis_bytes=None) -> dict[str, ResolvedFile]:
        """Loads every referenced file for one gateway request; any missing
        id fails the whole request rather than silently dropping a file the
        caller expects the model to see."""
        resolved: dict[str, ResolvedFile] = {}
        for file_id in sorted(file_ids):
            record = await self.get(application_id, file_id)
            if max_analysis_bytes is not None and record.bytes > max_analysis_bytes:
                raise FileTooLargeError("File exceeds the analysis size limit.", param="messages")
            record, data = await self.content(application_id, file_id)
            resolved[file_id] = ResolvedFile(
                file_id=file_id, filename=record.filename, mime_type=record.mime_type, data=data
            )
        return resolved

    # -- delete / expiry --------------------------------------------------------

    async def delete(self, application_id: str, file_id: str) -> GatewayFileRecord:
        record = await self.session.get(GatewayFileRecord, file_id)
        if record is None or record.application_id != application_id:
            raise FileNotFoundForApplicationError('File not found.', param='file_id')
        if record.deleted_at is not None:
            return record
        await self.get(application_id, file_id)
        await self._remove(record, action="gateway_file.deleted")
        await self.session.commit()
        try:
            await self.drain_deletions()
        except Exception:
            await self.session.rollback()
        return record

    async def purge_expired(self, *, application_id: str | None = None, limit: int = 100) -> int:
        """Deletes bytes of expired files and marks their rows. Runs on every
        upload for that application; schedule ``src/scripts/purge_expired_files.py``
        to sweep applications that stop uploading."""
        query = select(GatewayFileRecord).where(
            GatewayFileRecord.deleted_at.is_(None), GatewayFileRecord.expires_at <= utc_now()
        )
        if application_id is not None:
            query = query.where(GatewayFileRecord.application_id == application_id)
        expired = list(await self.session.scalars(query.limit(limit)))
        for record in expired:
            await self._remove(record, action="gateway_file.expired")
        if expired:
            await self.session.commit()
            await self.drain_deletions(limit=limit)
        return len(expired)

    async def _remove(self, record: GatewayFileRecord, *, action: str) -> None:
        record.deleted_at = utc_now()
        record.lifecycle = "pending_delete"
        if await self.session.get(FileDeletionOutbox, record.id) is None:
            self.session.add(FileDeletionOutbox(file_id=record.id, storage_backend=record.storage_backend,
                storage_key=record.storage_key))
        await self.audit.add_event(
            actor_user_id=None,
            action=action,
            resource_type="gateway_file",
            resource_id=record.id,
            metadata={"application_id": record.application_id},
        )


    async def drain_deletions(self, *, limit: int = 100) -> int:
        now, owner = utc_now(), str(uuid4())
        available = and_(
            or_(FileDeletionOutbox.lease_until.is_(None), FileDeletionOutbox.lease_until <= now),
            or_(FileDeletionOutbox.available_at.is_(None), FileDeletionOutbox.available_at <= now),
        )
        ids = list(await self.session.scalars(select(FileDeletionOutbox.file_id).where(
            FileDeletionOutbox.storage_backend == self.storage.name, available)
            .order_by(FileDeletionOutbox.created_at).limit(limit)))
        if not ids:
            await self.session.commit()
            return 0
        # Conditional UPDATE fences concurrent workers after either read has
        # committed. Copy immutable keys before ending the claim transaction.
        await self.session.execute(update(FileDeletionOutbox).where(
            FileDeletionOutbox.file_id.in_(ids), available).values(lease_owner=owner,
                lease_until=now + timedelta(minutes=10)))
        entries = list((await self.session.execute(select(FileDeletionOutbox.file_id, FileDeletionOutbox.storage_key)
            .where(FileDeletionOutbox.lease_owner == owner))).all())
        await self.session.commit()
        deleted = 0
        for file_id, storage_key in entries:
            try:
                await self.storage.delete(storage_key)
            except Exception:
                await self.session.execute(update(FileDeletionOutbox).where(
                    FileDeletionOutbox.file_id == file_id, FileDeletionOutbox.lease_owner == owner)
                    .values(attempts=FileDeletionOutbox.attempts + 1, lease_owner=None, lease_until=None))
            else:
                owned = await self.session.scalar(select(FileDeletionOutbox.file_id).where(
                    FileDeletionOutbox.file_id == file_id, FileDeletionOutbox.lease_owner == owner))
                if owned is not None:
                    await self.session.execute(update(GatewayFileRecord).where(GatewayFileRecord.id == file_id)
                        .values(lifecycle="deleted"))
                    await self.session.execute(delete(FileDeletionOutbox).where(
                        FileDeletionOutbox.file_id == file_id, FileDeletionOutbox.lease_owner == owner))
                    deleted += 1
            try:
                await self.session.commit()
            except Exception:
                await self.session.rollback()
                # A crashed owner leaves its durable lease for expiry/retry.
                break
        return deleted
