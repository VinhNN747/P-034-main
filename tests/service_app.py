"""Test-only edge dispatcher exercising both real deployment applications."""

from src.backend.main import app as management_app
from src.backend_ai.main import app as ai_app


class ServiceTestApp:
    def __init__(self):
        self.dependency_overrides = {}
        management_app.dependency_overrides = self.dependency_overrides
        ai_app.dependency_overrides = self.dependency_overrides

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        is_ai = (
            path in {"/api/v1/chat", "/api/v1/status", "/api/v1/gateway/analyze"}
            or path.startswith(("/api/v1/runtime/", "/internal/", "/v1/"))
            or (path == "/api/v1/gateway/requests" and scope.get("method") == "POST")
            or (path == "/api/v1/gateway/comparisons" and scope.get("method") == "POST")
        )
        target = ai_app if is_ai else management_app
        await target(scope, receive, send)


app = ServiceTestApp()
