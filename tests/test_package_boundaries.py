"""Ownership rules that keep management and AI independently deployable."""

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
AI_LIBRARIES = {"langchain", "langchain_core", "langchain_openai", "langchain_typesafe", "langgraph", "openai"}


def imported_modules(source: str, module_name: str, *, is_package: bool = False) -> list[tuple[int, str]]:
    """Resolve static imports and literal dynamic imports, including relatives."""
    dependencies = []
    package = module_name if is_package else module_name.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            dependencies.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, package)
            dependencies.append((node.lineno, base))
            dependencies.extend((node.lineno, f"{base}.{alias.name}") for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            name = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else ""
            argument = node.args[0]
            if name in {"import_module", "__import__"} and isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                dependencies.append((node.lineno, argument.value))
    return dependencies


@pytest.mark.parametrize(
    "owner,allowed_owners",
    [("backend", {"backend", "shared"}), ("backend_ai", {"backend_ai", "shared"}), ("shared", {"shared"})],
)
def test_source_dependencies_follow_service_ownership(owner, allowed_owners):
    root = SOURCE_ROOT / owner
    assert root.is_dir(), f"Missing ownership package: {owner}"
    sources = sorted(root.rglob("*.py"))
    assert sources, f"Empty ownership package: {owner}"
    violations = []
    for path in sources:
        relative = path.relative_to(SOURCE_ROOT.parent).with_suffix("")
        is_package = path.name == "__init__.py"
        module = ".".join(relative.parts[:-1] if is_package else relative.parts)
        for line, dependency in imported_modules(path.read_text(), module, is_package=is_package):
            parts = dependency.split(".")
            if parts[0] == "src" and len(parts) > 1 and parts[1] not in allowed_owners:
                violations.append(f"{path.relative_to(SOURCE_ROOT)}:{line} imports {dependency}")
            if owner != "backend_ai" and parts[0] in AI_LIBRARIES:
                violations.append(f"{path.relative_to(SOURCE_ROOT)}:{line} imports AI library {dependency}")
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "source",
    [
        "import src.backend_ai.gateway.factory",
        "from src import backend_ai",
        "from ...backend_ai.gateway import factory",
        'importlib.import_module("src.backend_ai.gateway.factory")',
        '__import__("src.backend_ai.gateway.factory")',
    ],
)
def test_boundary_scanner_detects_cross_service_dependencies(source):
    dependencies = imported_modules(source, "src.backend.services.catalog")
    assert any(name == "src.backend_ai" or name.startswith("src.backend_ai.") for _, name in dependencies)


def test_new_entry_points_register_owned_public_routes():
    script = '''
from src.backend.main import app as management
from src.backend_ai.main import app as ai
management_paths = management.openapi()["paths"]
ai_paths = ai.openapi()["paths"]
assert "/api/v1/applications" in management_paths
assert "/api/v1/applications" not in ai_paths
assert "get" in management_paths["/api/v1/gateway/requests"]
assert "post" not in management_paths["/api/v1/gateway/requests"]
assert "post" in ai_paths["/api/v1/gateway/requests"]
assert "get" not in ai_paths["/api/v1/gateway/requests"]
assert "/api/v1/runtime/requests" in ai_paths
assert "/api/v1/runtime/requests" not in management_paths
'''
    environment = dict(os.environ, DATABASE_URL="sqlite+aiosqlite:///:memory:", GATEWAY_EXECUTION_MODE="offline")
    result = subprocess.run([sys.executable, "-c", script], env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_relocated_settings_load_repository_root_env(tmp_path, monkeypatch):
    source = SOURCE_ROOT / "shared" / "config.py"
    assert source.is_file(), "Settings must live in shared without a dependency on either service"
    relocated = tmp_path / "src" / "shared" / "config.py"
    relocated.parent.mkdir(parents=True)
    relocated.write_text(source.read_text())
    (tmp_path / ".env").write_text(
        "DATABASE_URL=sqlite+aiosqlite:///root-env-test.db\nAPP_ENV=test\nAI_BACKEND_URL=http://ai-root-env:8001\n"
    )
    for name in ("DATABASE_URL", "APP_ENV", "AI_BACKEND_URL"):
        monkeypatch.delenv(name, raising=False)
    spec = importlib.util.spec_from_file_location("relocated_settings_probe", relocated)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    settings = module.Settings()
    assert settings.database_url == "sqlite+aiosqlite:///root-env-test.db"
    assert settings.app_env == "test"
    assert settings.ai_backend_url == "http://ai-root-env:8001"
