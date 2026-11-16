import logging
from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import InvalidTokenError, PyJWKClient

from src.shared.config import get_settings

ALLOWED_ALGORITHMS = ["RS256", "ES256"]
JWT_CLOCK_SKEW_SECONDS = 30
logger = logging.getLogger("uvicorn.error")


class InvalidAccessToken(Exception):  # noqa: N818
    pass


@dataclass(frozen=True)
class VerifiedClaims:
    user_id: str
    email: str | None
    authentication_complete: bool = True


@lru_cache
def _get_jwks_client(supabase_url: str) -> PyJWKClient:
    return PyJWKClient(f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json")


def get_signing_key(token: str):
    settings = get_settings()
    if not settings.supabase_url:
        raise InvalidAccessToken("Supabase authentication is not configured")
    client = _get_jwks_client(settings.supabase_url)
    return client.get_signing_key_from_jwt(token).key


def verify_access_token(token: str) -> VerifiedClaims:
    settings = get_settings()
    try:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            raise InvalidAccessToken("Unsupported token algorithm")
        payload = jwt.decode(
            token,
            get_signing_key(token),
            algorithms=[algorithm],
            audience=settings.supabase_jwt_audience,
            issuer=f"{settings.supabase_url.rstrip('/')}/auth/v1",
            leeway=JWT_CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iat", "sub"]},
        )
    except (InvalidTokenError, ValueError, KeyError) as exc:
        logger.warning("Access token verification failed: %s: %s", type(exc).__name__, exc)
        raise InvalidAccessToken(f"{type(exc).__name__}: {exc}") from exc
    methods = payload.get("amr", [])
    recovery_session = isinstance(methods, list) and any(
        isinstance(method, dict) and method.get("method") == "recovery" for method in methods
    )
    return VerifiedClaims(
        user_id=payload["sub"], email=payload.get("email"),
        authentication_complete=not recovery_session and payload.get("is_anonymous") is not True,
    )
