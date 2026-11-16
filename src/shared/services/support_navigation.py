"""Server-owned destinations: models suggest IDs, never arbitrary URLs."""

from typing import Literal

SupportNavigationId = Literal["create_application", "applications", "sdk_guide", "models", "model_pools", "requests", "usage", "billing"]

# id -> URL, Vietnamese label, English label, any permission required
SUPPORT_NAVIGATION = {
    "create_application": ("/applications/new", "Tạo application", "Create application", {"applications.write_own", "applications.manage_own"}),
    "applications": ("/applications", "Mở Applications / API keys", "Open Applications / API keys", {"applications.read_own"}),
    "sdk_guide": ("/sdk-guide", "Mở SDK Guide", "Open SDK Guide", {"applications.read_own"}),
    "models": ("/available-models", "Xem model khả dụng", "View available models", {"models.read_available"}),
    "model_pools": ("/model-pools", "Mở Model pools", "Open Model pools", {"model_pools.read_own"}),
    "requests": ("/requests", "Kiểm tra Requests", "Inspect Requests", {"requests.read_own"}),
    "usage": ("/usage", "Xem Usage", "View Usage", {"usage.read_own"}),
    "billing": ("/billing", "Mở Billing", "Open Billing", {"billing.read"}),
}
