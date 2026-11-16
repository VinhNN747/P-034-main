import importlib
import importlib.util
import subprocess
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from src.backend.main import app as management_app


def ai_app():
    assert importlib.util.find_spec("src.backend_ai.main") is not None, "AI needs its own application entry point"
    return importlib.import_module("src.backend_ai.main").app


def test_management_does_not_expose_ai_execution():
    paths = management_app.openapi()["paths"]
    assert "/api/v1/chat" not in paths
    assert "/api/v1/runtime/requests" not in paths
    assert "post" not in paths["/api/v1/gateway/requests"]
    assert "get" in paths["/api/v1/gateway/requests"]
    assert "/api/v1/support/chat" in paths


def test_ai_exposes_runtime_and_openai_api_without_management_mutations():
    paths = ai_app().openapi()["paths"]
    assert "/api/v1/applications" not in paths
    assert "/api/v1/billing/topups" not in paths
    assert "post" in paths["/api/v1/runtime/requests"]
    assert "get" in paths["/api/v1/runtime/requests/{request_id}"]
    assert "post" in paths["/api/v1/gateway/requests"]
    assert "get" not in paths["/api/v1/gateway/requests"]


def test_management_starts_without_ai_libraries():
    script = '''
import importlib.abc
import sys
class BlockAI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"langchain", "langchain_core", "langchain_openai", "langgraph", "langchain_typesafe"}:
            raise ImportError("Management imported AI dependency: " + fullname)
sys.meta_path.insert(0, BlockAI())
from src.shared import config
config.get_settings = lambda: config.Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:")
from src.backend.main import app
assert "/api/v1/applications" in app.openapi()["paths"]
assert "src.backend_ai.gateway.pipeline.request_pipeline" not in sys.modules, "Management must not eagerly load the AI pipeline"
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_both_services_have_health_without_auth():
    for app, expected_service in (
        (management_app, "AI20K Management API"),
        (ai_app(), "AI20K AI Runtime API"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            result = await client.get("/health")
            assert result.status_code == 200
            assert result.json()["status"] == "ok"
            assert result.json()["service"] == expected_service


@pytest.mark.asyncio
@pytest.mark.parametrize("token,configured_token,status", [(None, "trusted-token", 401), ("wrong-token", "trusted-token", 401), ("any-token", "", 503)])
async def test_internal_ai_endpoints_reject_untrusted_callers(monkeypatch, token, configured_token, status):
    from src.shared.config import Settings

    settings = Settings(_env_file=None, ai_service_token=configured_token)
    auth_module = importlib.import_module("src.backend_ai.dependencies.ai_service_auth") if importlib.util.find_spec(
        "src.backend_ai.dependencies.ai_service_auth"
    ) else None
    assert auth_module is not None, "Internal AI calls require service authentication"
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    headers = {"X-AI-Service-Token": token} if token else {}
    async with AsyncClient(transport=ASGITransport(app=ai_app()), base_url="http://test") as client:
        for path in ("/internal/v1/support/synthesize", "/internal/v1/models/model-1/probe"):
            result = await client.post(path, json={}, headers=headers)
            assert result.status_code == status
