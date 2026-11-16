from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from src.shared.config import Settings, get_settings

logger = logging.getLogger("uvicorn.error")


class SupabaseUserService:
    """Read the owner email from Supabase Auth without exposing admin keys."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    async def email_for_user(self, user_id: str) -> str | None:
        key = self.settings.supabase_secret_key or self.settings.supabase_service_role_key
        if not self.settings.supabase_url or not key:
            logger.warning("Supabase owner email lookup skipped: admin key or URL is not configured")
            return None

        url = f"{self.settings.supabase_url.rstrip('/')}/auth/v1/admin/users/{quote(user_id, safe='')}"
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("Supabase owner email lookup failed for %s: %s", user_id, exc)
            return None

        if response.status_code == 404:
            logger.warning("Supabase owner %s was not found", user_id)
            return None
        if response.is_error:
            logger.warning(
                "Supabase owner email lookup failed for %s with status %s",
                user_id,
                response.status_code,
            )
            return None

        try:
            payload = response.json()
        except ValueError:
            logger.warning("Supabase owner email lookup returned invalid JSON for %s", user_id)
            return None
        email = payload.get("email") if isinstance(payload, dict) else None
        return email.strip() if isinstance(email, str) and email.strip() else None


__all__ = ["SupabaseUserService"]
