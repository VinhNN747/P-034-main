"""The classifier as a service: src/classifier_service/app.py and the gateway's RemoteTaskClassifier."""

import importlib
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.backend_ai.gateway import factory
from src.backend_ai.gateway.understanding.onnx_inference import Prediction
from src.backend_ai.gateway.understanding.task_classifier import (
    ClassifierUnderstander,
    OnnxTaskClassifier,
    RemoteTaskClassifier,
)
from src.shared.config import Settings
from src.shared.dtos.gateway import EffectiveConstraints, GatewayRequest, RequestConstraints, TaskType

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t" * 40


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


class FakeModel:
    name = "onnx/model.onnx"

    def __init__(self, *args, **kwargs):
        self.calls = []

    @staticmethod
    def missing_files(model_dir, model_file="model.onnx"):
        return []

    def predict(self, text):
        self.calls.append(text)
        return Prediction("coding", 0.91, 0.42, {"coding": 0.91, "general": 0.09}, 12.3)


@pytest.fixture
def service(monkeypatch):
    for path in (ROOT / "src" / "classifier_service", ROOT / "src" / "backend_ai" / "gateway" / "understanding"):
        monkeypatch.syspath_prepend(str(path))
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    monkeypatch.setattr(module, "OnnxClassifierModel", FakeModel)
    monkeypatch.setenv("CLASSIFIER_TOKEN", TOKEN)
    yield module
    sys.modules.pop("app", None)


def test_service_classifies_with_the_token_and_refuses_without_it(service):
    with TestClient(service.app) as client:
        ok = client.post("/classify", json={"text": "Write a Go LRU cache"}, headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200
        assert ok.json() == {
            "task_type": "coding", "probability": 0.91, "difficulty": 0.42,
            "probabilities": {"coding": 0.91, "general": 0.09}, "model": "onnx/model.onnx", "latency_ms": 12.3,
        }
        assert client.post("/classify", json={"text": "x"}).status_code == 401
        assert client.post("/classify", json={"text": "x"}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        too_long = client.post("/classify", json={"text": "x" * 20_001}, headers={"Authorization": f"Bearer {TOKEN}"})
        assert too_long.status_code == 422
        assert client.get("/health").json() == {"status": "ok", "model": "onnx/model.onnx"}
        assert client.get("/docs").status_code == 404


def test_service_refuses_to_start_without_a_strong_token(service, monkeypatch):
    monkeypatch.setenv("CLASSIFIER_TOKEN", "short")
    with pytest.raises(RuntimeError, match="CLASSIFIER_TOKEN"):
        with TestClient(service.app):
            pass


def request(prompt="Write a Go LRU cache"):
    return GatewayRequest(request_id="remote-1", application_id="tests", prompt=prompt)


def constraints():
    return EffectiveConstraints(constraints=RequestConstraints(), resolved={})


@pytest.mark.asyncio
async def test_remote_classifier_calls_the_service_and_routes_on_its_answer(service):
    with TestClient(service.app) as client:
        def forward(req: httpx.Request) -> httpx.Response:
            response = client.post(req.url.path, content=req.content, headers=dict(req.headers))
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)

        remote = RemoteTaskClassifier("https://clf.example.run.app/", TOKEN, transport=httpx.MockTransport(forward))
        outcome = await ClassifierUnderstander(remote).understand(request(), constraints())

    assert not outcome.fallback
    assert outcome.analysis.task_types == [TaskType.CODING]
    assert outcome.analysis.routing_signals.difficulty == pytest.approx(0.42)
    assert outcome.details["task_classifier"]["model"] == "remote:onnx/model.onnx"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    lambda req: httpx.Response(401, json={"detail": "invalid token"}),
    lambda req: httpx.Response(503),
    lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("cold start", request=req)),
])
async def test_a_failing_or_cold_service_falls_back_to_the_rules(failure):
    remote = RemoteTaskClassifier("https://clf.example", TOKEN, transport=httpx.MockTransport(failure))
    outcome = await ClassifierUnderstander(remote).understand(request("Summarize this report."), constraints())

    assert outcome.fallback and outcome.strategy == "classifier->rule"
    assert outcome.analysis.task_types == [TaskType.SUMMARIZATION]


def test_the_url_selects_the_remote_classifier_without_shadow_or_local_load(monkeypatch):
    factory._remote_task_classifier.cache_clear()
    monkeypatch.setattr(factory, "_load_task_classifier", lambda *a: pytest.fail("must not load the local model"))
    remote = settings(gateway_execution_mode="online", gateway_task_classifier_url="https://clf.example",
                      gateway_task_classifier_token=TOKEN, gateway_task_classifier_dir="/unused")

    classifier = factory.build_task_classifier(remote)
    assert isinstance(classifier, RemoteTaskClassifier) and classifier.token == TOKEN
    assert factory.task_classifier_status(remote) == {
        "configured": True, "mode": "remote", "loaded": True, "model": "remote:https://clf.example", "error": None,
    }
    assert factory.build_pipeline(settings=remote).task_classifier_shadow is None
    assert TOKEN not in repr(remote)


@pytest.mark.asyncio
async def test_preload_does_nothing_for_a_remote_classifier(monkeypatch):
    monkeypatch.setattr(factory, "_task_classifier_load", lambda *a: pytest.fail("must not load the local model"))
    await factory.preload_task_classifier(settings(gateway_execution_mode="online",
                                                   gateway_task_classifier_url="https://clf.example",
                                                   gateway_task_classifier_required=True))


def test_local_classifier_keeps_its_shadow(monkeypatch):
    local = OnnxTaskClassifier.__new__(OnnxTaskClassifier)
    monkeypatch.setattr(factory, "build_task_classifier", lambda resolved: local)
    assert factory.build_pipeline(settings=settings()).task_classifier_shadow is local
