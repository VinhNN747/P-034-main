"""Request correlation for the public OpenAI-compatible API surface."""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def ensure_openai_request_id(request: Request) -> str:
    """Return the correlation id assigned before route dependencies run."""
    request_id = getattr(request.state, "request_id", None)
    if not request_id:
        request_id = str(uuid4())
        request.state.request_id = request_id
    return request_id


class OpenAIRequestContextMiddleware:
    """Pure ASGI middleware so streaming request bodies remain untouched."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope.get("type") != "http" or not (path == "/v1" or path.startswith("/v1/")):
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state.setdefault("request_id", str(uuid4()))

        started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = list(message.get("headers", []))
                present = {name.lower(): value for name, value in headers}
                request_id = present.get(b"x-request-id") or state.get("request_id", "").encode("ascii")
                if request_id:
                    if b"x-request-id" not in present:
                        headers.append((b"x-request-id", request_id))
                    if b"x-gateway-request-id" not in present:
                        headers.append((b"x-gateway-request-id", request_id))
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:
            if started:
                raise
            from src.backend_ai.routers.openai_errors import OpenAIAPIError, openai_error_handler
            error = exc if isinstance(exc, OpenAIAPIError) else OpenAIAPIError(
                500, "Internal server error.", type="server_error", code="internal_error",
                headers={"x-should-retry": "false"},
            )
            response = await openai_error_handler(Request(scope), error)
            await response(scope, receive, send_with_request_id)


def install_openai_request_context(app: FastAPI) -> None:
    """Attach IDs to every /v1 response, including auth and validation errors."""
    from src.backend_ai.services.openai_body_limits import OpenAIBodyLimitMiddleware
    app.add_middleware(OpenAIBodyLimitMiddleware)
    app.add_middleware(OpenAIRequestContextMiddleware)
