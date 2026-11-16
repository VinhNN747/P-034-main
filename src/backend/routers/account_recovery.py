from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import get_settings
from src.shared.database.session import get_db

router = APIRouter(prefix="/auth", tags=["account-recovery"])


class AccountEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if value.count("@") != 1 or any(character.isspace() for character in value):
            raise ValueError("Invalid email address")
        local, domain = value.split("@")
        if not local or not domain or "." not in domain:
            raise ValueError("Invalid email address")
        return value


class RecoveryEmail(AccountEmail):
    redirect_to: str = Field(min_length=1, max_length=2048)


class SignupRequest(AccountEmail):
    password: str = Field(min_length=8, max_length=128, repr=False)


@router.post("/signup/request")
async def request_signup_confirmation(
    payload: SignupRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    response: Response,
) -> dict[str, bool]:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_publishable_key:
        raise HTTPException(status_code=503, detail="mailUnavailable")
    try:
        confirmed = await session.scalar(text(
            "SELECT email_confirmed_at IS NOT NULL FROM auth.users WHERE lower(email) = :email "
            "AND deleted_at IS NULL LIMIT 1"
        ), {"email": payload.email})
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="signupUnavailable") from exc
    if confirmed:
        raise HTTPException(status_code=409, detail="emailAlreadyVerified")
    # Like password recovery, Auth owns code generation, expiry, rate limits
    # and email delivery. No session or activation is granted by this endpoint.
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            sent = await client.post(
                f"{settings.supabase_url.rstrip('/')}/auth/v1/signup",
                headers={"apikey": settings.supabase_publishable_key},
                json={"email": payload.email, "password": payload.password},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="mailUnavailable") from exc
    if sent.status_code == 429:
        raise HTTPException(status_code=429, detail="rateLimited", headers={"Retry-After": "60"})
    if not sent.is_success:
        code = "signupUnavailable"
        try:
            auth_error = sent.json()
            provider_code = auth_error.get("code") or auth_error.get("error_code")
            if provider_code in {"weak_password", "password_too_short", "password_too_long"}:
                code = "passwordTooWeak"
            elif provider_code in {"email_provider_disabled", "email_not_confirmed"}:
                code = "mailUnavailable"
        except (ValueError, AttributeError):
            pass
        raise HTTPException(status_code=503 if sent.status_code >= 500 else 400, detail=code)
    try:
        data = sent.json()
        user = data.get("user") or data
        if data.get("access_token") or data.get("session") or user.get("email_confirmed_at"):
            raise HTTPException(status_code=503, detail="confirmationRequired")
        if not user.get("id") or user.get("email", "").lower() != payload.email:
            raise HTTPException(status_code=503, detail="signupUnavailable")
        if user.get("identities") == []:
            raise HTTPException(status_code=409, detail="emailAlreadyVerified")
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="signupUnavailable") from exc
    response.headers["Cache-Control"] = "no-store"
    return {"pending": True}


@router.post("/signup/resend")
async def resend_signup_confirmation(
    payload: AccountEmail,
    session: Annotated[AsyncSession, Depends(get_db)],
    response: Response,
) -> dict[str, bool]:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_publishable_key:
        raise HTTPException(status_code=503, detail="mailUnavailable")
    try:
        status = await session.scalar(text(
            "SELECT CASE WHEN email_confirmed_at IS NULL THEN 'pending' ELSE 'confirmed' END "
            "FROM auth.users WHERE lower(email) = :email AND deleted_at IS NULL "
            "AND (banned_until IS NULL OR banned_until <= now()) LIMIT 1"
        ), {"email": payload.email})
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="signupUnavailable") from exc
    if status is None:
        raise HTTPException(status_code=404, detail="signupNotFound")
    if status != "pending":
        # Supabase returns HTTP 200 without sending mail for confirmed users.
        raise HTTPException(status_code=409, detail="emailAlreadyVerified")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            sent = await client.post(
                f"{settings.supabase_url.rstrip('/')}/auth/v1/resend",
                headers={"apikey": settings.supabase_publishable_key},
                json={"email": payload.email, "type": "signup"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="mailUnavailable") from exc
    if sent.status_code == 429:
        raise HTTPException(status_code=429, detail="rateLimited", headers={"Retry-After": "60"})
    if not sent.is_success:
        raise HTTPException(status_code=503, detail="mailUnavailable")
    response.headers["Cache-Control"] = "no-store"
    # Acceptance by Auth is not proof of delivery to the user's inbox.
    return {"requested": True}


def _is_allowed_recovery_redirect(redirect_to: str, cors_origins: str) -> bool:
    parsed = urlsplit(redirect_to)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/") != "/auth/confirm"
        or parsed.query
        or parsed.fragment
    ):
        return False
    redirect_origin = f"{parsed.scheme}://{parsed.netloc}".casefold()
    allowed_origins = {
        origin.strip().rstrip("/").casefold()
        for origin in cors_origins.split(",")
        if origin.strip()
    }
    return redirect_origin in allowed_origins


@router.post("/recovery")
async def request_password_recovery(
    payload: RecoveryEmail,
    session: Annotated[AsyncSession, Depends(get_db)],
    response: Response,
) -> dict[str, bool]:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_publishable_key:
        raise HTTPException(status_code=503, detail="mailUnavailable")
    if not _is_allowed_recovery_redirect(payload.redirect_to, settings.cors_origins):
        raise HTTPException(status_code=400, detail="invalidRedirect")
    try:
        exists = await session.scalar(text(
            "SELECT 1 FROM auth.users WHERE lower(email) = :email "
            "AND deleted_at IS NULL AND (banned_until IS NULL OR banned_until <= now()) LIMIT 1"
        ), {"email": payload.email})
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="recoveryUnavailable") from exc
    if exists is None:
        raise HTTPException(status_code=404, detail="emailNotFound")

    # Supabase owns the recovery token, its expiry, rate limits and mail delivery.
    # Neither the token nor a password is stored by the management backend.
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            sent = await client.post(
                f"{settings.supabase_url.rstrip('/')}/auth/v1/recover",
                headers={"apikey": settings.supabase_publishable_key},
                json={"email": payload.email},
                params={"redirect_to": payload.redirect_to},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="mailUnavailable") from exc
    if sent.status_code == 429:
        raise HTTPException(status_code=429, detail="rateLimited", headers={"Retry-After": "60"})
    if not sent.is_success:
        raise HTTPException(status_code=503, detail="mailUnavailable")
    response.headers["Cache-Control"] = "no-store"
    return {"sent": True}
