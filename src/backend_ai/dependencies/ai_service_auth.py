import secrets
from typing import Annotated

from fastapi import Header, HTTPException

from src.shared.config import get_settings


async def require_ai_service_token(
    token: Annotated[str | None, Header(alias="X-AI-Service-Token")] = None,
) -> None:
    expected = get_settings().ai_service_token
    if not expected:
        raise HTTPException(status_code=503, detail="Internal AI service is not configured")
    if token is None or not secrets.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid service credentials")
