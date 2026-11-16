from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.repositories.application_keys import ApplicationKeyRepository

# ak_ = an application (online pipeline), off_ = an offline pipeline.
_KEY_PATTERN = re.compile(r"^(?:ak|off)_[A-Za-z0-9_-]{32,253}$")


@dataclass(frozen=True)
class ApplicationPrincipal:
    application_id: str
    key_id: str
    pipeline: str = "online"
    """"offline" for an offline pipeline's key (off_...): the request runs the offline pipeline."""


class InvalidApplicationKeyError(Exception):
    """The key is missing, malformed, unknown, mismatched, or revoked."""


class InactiveApplicationError(Exception):
    """A valid key belongs to an application that cannot execute requests."""


class ApplicationKeyAuthService:
    def __init__(self, session: AsyncSession):
        self.repository = ApplicationKeyRepository(session)

    async def authenticate(self, raw_key: str) -> ApplicationPrincipal:
        if not isinstance(raw_key, str) or len(raw_key) > 256 or not _KEY_PATTERN.fullmatch(raw_key):
            raise InvalidApplicationKeyError

        record = await self.repository.get_by_prefix(raw_key[:12])
        if record is None:
            raise InvalidApplicationKeyError
        supplied_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(supplied_hash, record.key_hash) or record.revoked_at is not None:
            raise InvalidApplicationKeyError

        application = await self.repository.get_application(record.application_id)
        if application is None or application.status != "active":
            raise InactiveApplicationError
        # The prefix is issued from the pipeline; a mismatch means a forged or misfiled key.
        if raw_key.startswith("off_") != (getattr(application, "pipeline", "online") == "offline"):
            raise InvalidApplicationKeyError

        await self.repository.touch_last_used(record.id)
        return ApplicationPrincipal(
            application_id=record.application_id,
            key_id=record.id,
            pipeline="offline" if raw_key.startswith("off_") else "online",
        )
