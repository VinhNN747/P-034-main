"""Non-generative Anthropic input token counting for budget estimates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_TOKEN_COUNT_FIELDS = frozenset({"model", "messages", "system", "tools", "thinking", "output_config", "tool_choice"})


async def count_anthropic_tokens(client: Any, request: Mapping[str, Any]) -> int | None:
    """Return the official Messages token count, or None when it is unavailable.

    This endpoint counts a request without creating a model response. Failure is
    deliberately non-fatal; callers must then use conservative pricing.
    """
    messages_api = getattr(client, "messages", None)
    counter = getattr(messages_api, "count_tokens", None)
    if not callable(counter):
        return None
    payload = {key: request[key] for key in _TOKEN_COUNT_FIELDS if key in request}
    try:
        result = await counter(**payload)
    except Exception:
        return None
    tokens = getattr(result, "input_tokens", None)
    return tokens if type(tokens) is int and tokens >= 0 else None
