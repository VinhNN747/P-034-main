import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.core.security import InvalidAccessToken, verify_access_token
from src.shared.database.session import get_db
from src.shared.repositories.authorization import get_authorization_profile

bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str | None
    roles: tuple[str, ...]
    permissions: frozenset[str]
    authentication_complete: bool = True


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUser:
    if credentials is None:
        logger.warning("Authorization request did not include a Bearer token")
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = verify_access_token(credentials.credentials)
    except InvalidAccessToken as exc:
        logger.warning("Authorization request rejected: %s", exc)
        raise HTTPException(
            status_code=401, detail="Invalid or expired access token", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    try:
        email_verified = await session.scalar(text(
            "SELECT email_confirmed_at IS NOT NULL FROM auth.users WHERE id = :user_id "
            "AND deleted_at IS NULL AND (banned_until IS NULL OR banned_until <= now()) LIMIT 1"
        ), {"user_id": claims.user_id})
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Account verification unavailable") from exc
    if not email_verified:
        raise HTTPException(status_code=403, detail="Email confirmation required")
    profile = await get_authorization_profile(session, claims.user_id)
    return CurrentUser(
        id=claims.user_id, email=claims.email, roles=profile.roles, permissions=profile.permissions,
        authentication_complete=claims.authentication_complete,
    )


def require_permission(permission: str) -> Callable:
    return require_any_permission(permission)


def require_any_permission(*permissions: str) -> Callable:
    if not permissions:
        raise ValueError("At least one permission is required")

    async def dependency(current_user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
        if not any(permission in current_user.permissions for permission in permissions):
            raise HTTPException(status_code=403, detail="Insufficient role")
        return current_user

    return dependency
