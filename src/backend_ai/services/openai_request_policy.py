"""Caller identity is necessary to replay an ambiguous generation safely."""

from typing import Literal
from uuid import uuid4

from fastapi import Request

from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.backend_ai.services.runtime_operation_service import InvalidIdempotencyKeyError, RuntimeOperationService
from src.shared.config import get_settings

IdempotencyPolicy = Literal["strict", "best_effort"]


def resolve_openai_idempotency_key(request: Request, policy: IdempotencyPolicy | None = None) -> str:
    policy = policy or get_settings().gateway_openai_idempotency_policy
    key = request.headers.get("Idempotency-Key")
    if key is None:
        if policy == "strict":
            raise OpenAIAPIError(
                400,
                "Idempotency-Key is required for generation requests.",
                code="missing_idempotency_key",
                headers={"x-should-retry": "false"},
            )
        key = str(uuid4())
    try:
        RuntimeOperationService.validate_idempotency_key(key)
    except InvalidIdempotencyKeyError as exc:
        raise OpenAIAPIError(
            400,
            "Idempotency-Key must contain 1 to 128 visible ASCII characters.",
            code="invalid_idempotency_key",
            headers={"x-should-retry": "false"},
        ) from exc
    request.state.idempotency_key = key
    return key
