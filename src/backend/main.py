"""Management backend: uvicorn src.backend.main:app --port 8000."""

from src.backend.routers.account_recovery import router as account_recovery_router
from src.backend.routers.application_keys import router as application_keys_router
from src.backend.routers.applications import router as applications_router
from src.backend.routers.authorization import router as authorization_router
from src.backend.routers.billing import router as billing_router
from src.backend.routers.customer_endpoints import router as customer_endpoints_router
from src.backend.routers.gateway_catalog import router as gateway_catalog_router
from src.backend.routers.gateway_comparisons import router as gateway_comparisons_router
from src.backend.routers.gateway_configuration import router as gateway_configuration_router
from src.backend.routers.gateway_observability import router as gateway_observability_router
from src.backend.routers.offline_pipelines import router as offline_pipelines_router
from src.backend.routers.platform import router as platform_router
from src.backend.routers.policy_reviews import router as policy_reviews_router
from src.backend.routers.provider_connections import router as provider_connections_router
from src.backend.routers.support_chat import router as support_chat_router
from src.shared.core.application import create_application

app = create_application("AI20K Management API")

for router in (
    account_recovery_router,
    applications_router,
    application_keys_router,
    gateway_catalog_router,
    provider_connections_router,
    customer_endpoints_router,
    offline_pipelines_router,
    gateway_configuration_router,
    gateway_observability_router,
    gateway_comparisons_router,
    billing_router,
    policy_reviews_router,
    authorization_router,
    platform_router,
    support_chat_router,
):
    app.include_router(router, prefix="/api/v1")
