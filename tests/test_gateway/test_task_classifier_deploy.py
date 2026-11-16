"""Putting the task classifier on a server: load reasons, startup preload, /health, and the download script."""

import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from src.backend_ai.gateway import factory
from src.backend_ai.gateway.understanding.task_classifier import OnnxTaskClassifier
from src.shared.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def load_fetch_script():
    spec = importlib.util.spec_from_file_location("fetch_task_classifier", ROOT / "src" / "scripts" / "fetch_task_classifier.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.fixture(autouse=True)
def clear_classifier_cache():
    factory._load_task_classifier.cache_clear()
    yield
    factory._load_task_classifier.cache_clear()


def write_model_dir(path: Path, model: bytes = b"not an onnx graph") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "labels.json").write_text(json.dumps({"tasks": ["coding"], "max_len": 8}), encoding="utf-8")
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    (path / "model.onnx").write_bytes(model)
    return path


def test_try_load_says_why_the_classifier_is_disabled(tmp_path):
    assert OnnxTaskClassifier.try_load(None) == (None, "GATEWAY_TASK_CLASSIFIER_DIR is not set")
    classifier, reason = OnnxTaskClassifier.try_load(tmp_path)
    assert classifier is None and "labels.json, tokenizer.json, model.onnx missing" in reason


def test_a_corrupt_model_file_disables_the_classifier_instead_of_raising(tmp_path):
    classifier, reason = OnnxTaskClassifier.try_load(write_model_dir(tmp_path))
    assert classifier is None
    assert reason  # onnxruntime/tokenizers missing, or the file cannot be parsed: either way not loaded


def test_status_reports_not_configured_and_load_failures(tmp_path):
    assert factory.task_classifier_status(settings()) == {
        "configured": False, "mode": None, "loaded": False, "model": None, "error": None,
    }
    status = factory.task_classifier_status(settings(gateway_task_classifier_dir=str(tmp_path)))
    assert status["configured"] and status["mode"] == "local" and not status["loaded"]
    assert "missing" in status["error"]


@pytest.mark.asyncio
async def test_preload_refuses_to_start_when_the_classifier_is_required(tmp_path):
    await factory.preload_task_classifier(settings())  # not configured: nothing to do
    await factory.preload_task_classifier(settings(gateway_task_classifier_dir=str(tmp_path)))  # optional: rules
    with pytest.raises(RuntimeError, match="GATEWAY_TASK_CLASSIFIER_REQUIRED"):
        await factory.preload_task_classifier(settings(gateway_task_classifier_required=True))
    with pytest.raises(RuntimeError, match="missing"):
        await factory.preload_task_classifier(
            settings(gateway_task_classifier_dir=str(tmp_path), gateway_task_classifier_required=True)
        )


def test_health_marks_a_configured_but_unloaded_classifier_as_degraded(monkeypatch):
    from src.backend_ai import main

    monkeypatch.setattr(main, "task_classifier_status", lambda: {"configured": True, "loaded": False, "model": None, "error": "x"})
    assert main.ai_health_details()["status"] == "degraded"
    monkeypatch.setattr(main, "task_classifier_status", lambda: {"configured": False, "loaded": False, "model": None, "error": None})
    assert "status" not in main.ai_health_details()


def test_the_source_token_is_kept_out_of_settings_repr():
    assert "hf_secret" not in repr(settings(gateway_task_classifier_source_token="hf_secret"))


class Store:
    """A Hugging Face-like repo: small files served directly, model.onnx redirected to a CDN host."""

    def __init__(self, files: dict[str, bytes], *, manifest: dict | None = None):
        self.files = files
        self.manifest = manifest or {
            "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for name, data in files.items()}
        }
        self.requests: list[httpx.Request] = []
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("store down", request=request)
        name = request.url.path.rsplit("/", 1)[-1]
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=self.files[name])
        if name == "manifest.json":
            return httpx.Response(200, json=self.manifest)
        if name == "model.onnx" and name in self.files:
            return httpx.Response(302, headers={"location": f"https://cdn.example/signed/{name}?sig=1"})
        return httpx.Response(200, content=self.files[name]) if name in self.files else httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


FILES = {"labels.json": b'{"tasks": [], "max_len": 8}', "tokenizer.json": b"{}", "model.onnx": b"weights" * 1000}
REPO = "https://huggingface.co/user/clf/resolve/main"


def test_fetch_downloads_verifies_and_skips_files_already_present(tmp_path):
    script = load_fetch_script()
    store = Store(FILES)
    target = tmp_path / "onnx"

    assert script.fetch(target, REPO + "/", "hf_secret", "model.onnx", store.transport()) == 0
    assert {name: (target / name).read_bytes() for name in FILES} == FILES
    assert str(store.requests[0].url) == f"{REPO}/manifest.json"
    assert store.requests[0].headers["authorization"] == "Bearer hf_secret"
    assert not list(target.glob("*.part"))

    store.requests.clear()
    assert script.fetch(target, REPO, "hf_secret", "model.onnx", store.transport()) == 0
    assert [request.url.path for request in store.requests] == ["/user/clf/resolve/main/manifest.json"]  # nothing re-downloaded


def test_the_token_is_not_sent_to_the_cdn_after_a_redirect(tmp_path):
    script = load_fetch_script()
    store = Store(FILES)

    assert script.fetch(tmp_path / "onnx", REPO, "hf_secret", "model.onnx", store.transport()) == 0
    [cdn] = [request for request in store.requests if request.url.host == "cdn.example"]
    assert "authorization" not in cdn.headers


def test_fetch_rejects_a_file_that_does_not_match_the_manifest(tmp_path):
    script = load_fetch_script()
    store = Store(FILES)
    store.files = {**FILES, "model.onnx": b"tampered"}
    target = tmp_path / "onnx"

    assert script.fetch(target, REPO, None, "model.onnx", store.transport()) == 1
    assert not (target / "model.onnx").exists()
    assert not list(target.glob("*.part"))


def test_fetch_keeps_a_verified_copy_when_the_store_is_down(tmp_path):
    script = load_fetch_script()
    store = Store(FILES)
    target = tmp_path / "onnx"
    assert script.fetch(target, REPO, None, "model.onnx", store.transport()) == 0

    store.down = True
    assert script.fetch(target, REPO, None, "model.onnx", store.transport()) == 0
    (target / "model.onnx").write_bytes(b"truncated")
    assert script.fetch(target, REPO, None, "model.onnx", store.transport()) == 1


def test_fetch_requires_the_model_in_the_manifest(tmp_path):
    script = load_fetch_script()
    store = Store({"labels.json": b"{}", "tokenizer.json": b"{}"})
    assert script.fetch(tmp_path, REPO, None, "model.onnx", store.transport()) == 1


def test_external_weights_are_listed_and_downloaded(tmp_path):
    script = load_fetch_script()
    files = {**FILES, "model.onnx": b"graph", "model.onnx.data": b"weights" * 500}
    source = tmp_path / "export"
    source.mkdir()
    for name, data in files.items():
        (source / name).write_bytes(data)

    assert script.write_manifest(source, "model.onnx") == 0
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == Store(files).manifest
    target = tmp_path / "server"
    assert script.fetch(target, REPO, None, "model.onnx", Store(files).transport()) == 0
    assert (target / "model.onnx.data").read_bytes() == files["model.onnx.data"]


def test_write_manifest_matches_what_fetch_verifies(tmp_path):
    script = load_fetch_script()
    source = tmp_path / "export"
    source.mkdir()
    for name, data in FILES.items():
        (source / name).write_bytes(data)

    assert script.write_manifest(source, "model.onnx") == 0
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == Store(FILES).manifest
