from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.application_keys import (
    ApplicationKeyCreate,
    ApplicationKeyCreated,
    ApplicationKeyPage,
    ApplicationKeyRead,
)
from src.shared.models.core import Application
from src.shared.models.integration import ApplicationApiKeyRecord
from src.shared.repositories.audit import AuditRepository


class ApplicationKeyNotFoundError(Exception):
    pass


class ApplicationKeyService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.audit = AuditRepository(session)

    async def list(
        self, owner_user_id: str, application_id: str, *, page: int = 1, page_size: int = 20
    ) -> ApplicationKeyPage:
        await self._application(owner_user_id, application_id)
        condition = ApplicationApiKeyRecord.application_id == application_id
        total = int(
            await self.session.scalar(select(func.count()).select_from(ApplicationApiKeyRecord).where(condition)) or 0
        )
        rows = list(
            await self.session.scalars(
                select(ApplicationApiKeyRecord)
                .where(condition)
                .order_by(ApplicationApiKeyRecord.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return ApplicationKeyPage(items=[self._read(row) for row in rows], page=page, page_size=page_size, total=total)

    async def create(
        self, owner_user_id: str, application_id: str, payload: ApplicationKeyCreate
    ) -> ApplicationKeyCreated:
        application = await self._application(owner_user_id, application_id)
        # An offline pipeline's key says so on sight (and in logs): off_..., an application's ak_...
        raw_key = ("off_" if application.pipeline == "offline" else "ak_") + secrets.token_urlsafe(32)
        record = ApplicationApiKeyRecord(
            application_id=application_id,
            name=payload.name,
            description=payload.description,
            key_prefix=raw_key[:12],
            key_hash=hashlib.sha256(raw_key.encode("utf-8")).hexdigest(),
        )
        self.session.add(record)
        await self.session.flush()
        await self.audit.add_event(
            actor_user_id=owner_user_id,
            action="application_api_key.created",
            resource_type="application_api_key",
            resource_id=record.id,
            metadata={"key_prefix": record.key_prefix, "application_id": application_id},
        )
        await self.session.commit()
        await self.session.refresh(record)
        return ApplicationKeyCreated(**self._read(record).model_dump(), raw_key=raw_key)

    async def revoke(self, owner_user_id: str, application_id: str, key_id: str) -> ApplicationKeyRead:
        await self._application(owner_user_id, application_id)
        record = await self.session.scalar(
            select(ApplicationApiKeyRecord).where(
                ApplicationApiKeyRecord.id == key_id,
                ApplicationApiKeyRecord.application_id == application_id,
            )
        )
        if record is None:
            raise ApplicationKeyNotFoundError("Application API key not found")
        if record.revoked_at is None:
            from src.shared.models.core import utc_now

            record.revoked_at = utc_now()
            await self.audit.add_event(
                actor_user_id=owner_user_id,
                action="application_api_key.revoked",
                resource_type="application_api_key",
                resource_id=record.id,
                metadata={"key_prefix": record.key_prefix, "application_id": application_id},
            )
            await self.session.commit()
            await self.session.refresh(record)
        return self._read(record)

    async def _application(self, owner_user_id: str, application_id: str) -> Application:
        application = await self.session.scalar(
            select(Application).where(
                Application.id == application_id,
                Application.owner_user_id == owner_user_id,
            )
        )
        if application is None:
            raise ApplicationKeyNotFoundError("Application not found")
        return application

    @staticmethod
    def _read(record: ApplicationApiKeyRecord) -> ApplicationKeyRead:
        return ApplicationKeyRead(
            id=record.id,
            name=record.name,
            description=record.description,
            key_prefix=record.key_prefix,
            created_at=record.created_at,
            last_used_at=record.last_used_at,
            revoked_at=record.revoked_at,
            revoked=record.revoked_at is not None,
        )
