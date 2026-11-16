from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.core import Application, utc_now
from src.shared.models.integration import ApplicationApiKeyRecord


class ApplicationKeyRepository:
    """Queries used by the application-key authentication flow."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_prefix(self, key_prefix: str) -> ApplicationApiKeyRecord | None:
        return await self.session.scalar(
            select(ApplicationApiKeyRecord).where(ApplicationApiKeyRecord.key_prefix == key_prefix)
        )

    async def get_application(self, application_id: str) -> Application | None:
        return await self.session.get(Application, application_id)

    async def touch_last_used(self, key_id: str) -> None:
        await self.session.execute(
            update(ApplicationApiKeyRecord)
            .where(ApplicationApiKeyRecord.id == key_id, ApplicationApiKeyRecord.revoked_at.is_(None))
            .values(last_used_at=utc_now())
        )
        await self.session.commit()
