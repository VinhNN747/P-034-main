from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.services.application_auth_service import (
    ApplicationKeyAuthService,
    ApplicationPrincipal,
    InactiveApplicationError,
    InvalidApplicationKeyError,
)
from src.shared.database.session import get_db
from src.shared.dtos.runtime_api import RuntimeAPIError

application_bearer_scheme = HTTPBearer(auto_error=False)


async def get_application_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(application_bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ApplicationPrincipal:
    if credentials is None:
        raise RuntimeAPIError(
            401,
            "AUTHENTICATION_FAILED",
            "A valid gateway API key is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await ApplicationKeyAuthService(session).authenticate(credentials.credentials)
    except InvalidApplicationKeyError as exc:
        raise RuntimeAPIError(
            401,
            "AUTHENTICATION_FAILED",
            "A valid gateway API key is required.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except InactiveApplicationError as exc:
        raise RuntimeAPIError(403, "APPLICATION_INACTIVE", "The application is inactive.") from exc
