"""Application-key authentication for the OpenAI-compatible ``/v1`` API.

Same checks as the runtime API (``ApplicationKeyAuthService``: key format,
constant-time hash compare, revocation, active application, last-used
touch); only the error envelope differs, so OpenAI SDKs can read it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.backend_ai.services.application_auth_service import (
    ApplicationKeyAuthService,
    ApplicationPrincipal,
    InactiveApplicationError,
    InvalidApplicationKeyError,
)
from src.shared.config import get_settings
from src.shared.database.session import get_db

_bearer = HTTPBearer(auto_error=False)


async def get_openai_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ApplicationPrincipal:
    if not get_settings().gateway_runtime_enabled:
        raise OpenAIAPIError(503, "The gateway runtime is not enabled.", type="server_error", code="runtime_disabled")
    if credentials is None or not credentials.credentials:
        raise OpenAIAPIError(
            401,
            "You didn't provide an API key. Send 'Authorization: Bearer <application key>'.",
            type="authentication_error",
            code="invalid_api_key",
        )
    try:
        return await ApplicationKeyAuthService(session).authenticate(credentials.credentials)
    except InvalidApplicationKeyError as exc:
        raise OpenAIAPIError(
            401, "Incorrect API key provided.", type="authentication_error", code="invalid_api_key"
        ) from exc
    except InactiveApplicationError as exc:
        raise OpenAIAPIError(
            403, "The application for this API key is not active.", type="permission_error", code="application_inactive"
        ) from exc
    except SQLAlchemyError as exc:
        await session.rollback()
        raise OpenAIAPIError(
            503, "Gateway authentication storage is unavailable.", type="server_error", code="storage_unavailable"
        ) from exc
