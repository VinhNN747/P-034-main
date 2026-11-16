"""Shared HTTP setup; each composition root registers only its own routers."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.shared.config import get_settings
from src.shared.database.session import engine
from src.shared.dtos.runtime_api import RuntimeAPIError, runtime_api_error_handler
from src.shared.services.credential_crypto import ensure_credential_crypto


def create_application(
    title: str,
    *,
    runtime_context: Callable[[FastAPI], AbstractAsyncContextManager] | None = None,
    health_details: Callable[[], dict[str, Any]] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            ensure_credential_crypto(get_settings())
            async with AsyncExitStack() as stack:
                if runtime_context is not None:
                    await stack.enter_async_context(runtime_context(app))
                yield
        finally:
            await engine.dispose()

    app = FastAPI(title=title, version="1.0.0", lifespan=lifespan)
    app.add_exception_handler(RuntimeAPIError, runtime_api_error_handler)

    @app.exception_handler(RequestValidationError)
    async def secret_safe_validation_handler(request: Request, exc: RequestValidationError):
        sensitive_routes = ("/api/v1/gateway/provider-connections", "/api/v1/gateway/providers")
        if request.url.path.startswith(sensitive_routes):
            errors = [
                {key: error[key] for key in ("loc", "msg", "type") if key in error}
                for error in exc.errors()
            ]
            return JSONResponse(status_code=422, content={"detail": errors})
        return await request_validation_exception_handler(request, exc)
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        # Include the composition-root identity so operators can detect a
        # management process accidentally bound to the AI service port.
        body: dict[str, Any] = {"status": "ok", "env": get_settings().app_env, "service": title}
        if health_details is not None:
            body.update(health_details())
        return body

    return app
