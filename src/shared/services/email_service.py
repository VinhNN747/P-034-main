from __future__ import annotations

import httpx

from src.shared.config import Settings, get_settings


class EmailDeliveryNotConfiguredError(Exception):
    pass


class EmailDeliveryError(Exception):
    pass


class ResendEmailService:
    """Small server-side adapter for the Resend email API."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    async def send(
        self,
        *,
        recipient: str,
        subject: str,
        html: str,
        text: str,
    ) -> str:
        if not self.settings.resend_api_key or not self.settings.resend_from_email:
            raise EmailDeliveryNotConfiguredError("Resend email delivery is not configured")

        sender = self.settings.resend_from_email
        if self.settings.resend_from_name:
            sender = f"{self.settings.resend_from_name} <{sender}>"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {self.settings.resend_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": sender,
                        "to": [recipient],
                        "subject": subject,
                        "html": html,
                        "text": text,
                    },
                )
        except httpx.HTTPError as exc:
            raise EmailDeliveryError("Resend request failed") from exc

        if response.is_error:
            raise EmailDeliveryError(f"Resend returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise EmailDeliveryError("Resend returned an invalid response") from exc
        message_id = body.get("id")
        if not message_id:
            raise EmailDeliveryError("Resend response did not include a message id")
        return str(message_id)


__all__ = ["EmailDeliveryError", "EmailDeliveryNotConfiguredError", "ResendEmailService"]
