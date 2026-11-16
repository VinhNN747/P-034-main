"""AI and OpenAI-compatible API: uvicorn src.backend_ai.main:app --port 8001."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.backend_ai.gateway.factory import preload_task_classifier, task_classifier_status
from src.backend_ai.gateway.pipeline_profile import configuration_issues, pipeline_profile
from src.backend_ai.gateway.pipeline_profile import log_configuration as log_pipeline_configuration
from src.backend_ai.routers.ai_internal import router as internal_router
from src.backend_ai.routers.chat import router as chat_router
from src.backend_ai.routers.comparisons import router as comparisons_router
from src.backend_ai.routers.gateway_runtime import router as gateway_runtime_router
from src.backend_ai.routers.openai_compat import router as openai_compat_router
from src.backend_ai.routers.openai_embeddings import router as openai_embeddings_router
from src.backend_ai.routers.openai_errors import (
    OpenAIAPIError,
    is_openai_path,
    openai_error_handler,
    validation_error_to_openai,
)
from src.backend_ai.routers.openai_files import router as openai_files_router
from src.backend_ai.routers.openai_responses import router as openai_responses_router
from src.backend_ai.routers.runtime import router as runtime_router
from src.backend_ai.services.openai_request_context import install_openai_request_context
from src.backend_ai.workers.lifespan import comparison_lifespan
from src.shared.config import get_settings
from src.shared.core.application import create_application


@asynccontextmanager
async def ai_runtime_lifespan(app: FastAPI):
    log_pipeline_configuration(get_settings())  # raises on an invalid pipeline configuration
    await preload_task_classifier()
    async with comparison_lifespan(app):
        yield


def ai_health_details() -> dict:
    settings = get_settings()
    classifier = task_classifier_status()
    issues = configuration_issues(settings)
    # Still HTTP 200: the gateway serves on the rules, but operators see it is not on the model. A pipeline
    # issue alone (e.g. offline without a classifier) is listed, not degraded: the rules still serve it.
    degraded = classifier["configured"] and not classifier["loaded"]
    return {
        "pipeline": pipeline_profile(settings).name,
        "pipeline_issues": [{"level": level, "message": message} for level, message in issues],
        "task_classifier": classifier,
        **({"status": "degraded"} if degraded else {}),
    }


app = create_application(
    "AI20K AI Runtime API", runtime_context=ai_runtime_lifespan, health_details=ai_health_details
)
install_openai_request_context(app)
app.include_router(chat_router, prefix="/api/v1")
app.include_router(gateway_runtime_router, prefix="/api/v1")
app.include_router(runtime_router, prefix="/api/v1")
app.include_router(internal_router)
app.include_router(comparisons_router, prefix="/api/v1")
# OpenAI-compatible surface at /v1 so OpenAI SDKs work with base_url=<host>/v1.
app.include_router(openai_compat_router)
app.include_router(openai_files_router)
app.include_router(openai_embeddings_router)
app.include_router(openai_responses_router)
app.add_exception_handler(OpenAIAPIError, openai_error_handler)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    if is_openai_path(request):
        return await openai_error_handler(request, validation_error_to_openai(exc))
    return await request_validation_exception_handler(request, exc)




@app.exception_handler(StarletteHTTPException)
async def openai_http_error_handler(request: Request, exc: StarletteHTTPException):
    if is_openai_path(request):
        return await openai_error_handler(request, OpenAIAPIError(
            exc.status_code, str(exc.detail), code={404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error"),
            headers=exc.headers,
        ))
    return await http_exception_handler(request, exc)
