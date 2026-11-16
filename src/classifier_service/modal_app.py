"""Deploy classifier_service (app.py) to Modal: serverless CPU, HTTPS URL, scales to zero.

    pip install modal && modal setup                               # log in (browser)
    modal secret create huggingface HF_TOKEN=hf_...                # read-only token for the private model repo
    modal secret create classifier-token CLASSIFIER_TOKEN=<random, >= 32 chars>
    modal deploy src/classifier_service/modal_app.py               # prints https://<workspace>--task-classifier-web.modal.run

The model is downloaded from Hugging Face once, while Modal builds the image (inside Modal's network), and is
baked in; the HF token is only available to that build step. Then set on the gateway:
GATEWAY_TASK_CLASSIFIER_URL=<that URL> and GATEWAY_TASK_CLASSIFIER_TOKEN=<the same CLASSIFIER_TOKEN>.

Cost (Modal list price, 2026): 0.5 physical core (= 1 vCPU) + 1 GiB is about $0.032/hour while a container runs,
so even an always-on container (CLASSIFIER_MIN_CONTAINERS=1, ~$23/month) fits the Starter plan's $30 monthly credit.
"""

from __future__ import annotations

import os
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parents[2]
# Pin a commit: a new model is a new commit here plus `modal deploy` (the image rebuilds when it changes).
MODEL_URL = "https://huggingface.co/MAITUNG/ai20k-task-classifier/resolve/afb473de7145c39157c1832fa12f2f5477dacfbd"
# Deploy-time settings: 0 = scale to zero (first request after idle waits for a cold start), 1 = always warm.
MIN_CONTAINERS = int(os.environ.get("CLASSIFIER_MIN_CONTAINERS", "0"))


def download_model(model_url: str) -> None:
    """Runs once in the image build: the same verified download as the gateway (src/scripts/fetch_task_classifier.py)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("fetch", "/build/scripts/fetch_task_classifier.py")
    fetch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetch)
    if fetch.fetch(Path("/model"), model_url, os.environ["HF_TOKEN"], "model.onnx") != 0:
        raise RuntimeError("model download failed")


image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_requirements(str(ROOT / "src" / "classifier_service" / "requirements.txt"))
    .pip_install("httpx==0.28.1")
    .add_local_file(ROOT / "src" / "scripts" / "fetch_task_classifier.py", "/build/scripts/fetch_task_classifier.py", copy=True)
    .run_function(
        download_model,
        secrets=[modal.Secret.from_name("huggingface", required_keys=["HF_TOKEN"])],
        kwargs={"model_url": MODEL_URL},
    )
    .env({"CLASSIFIER_MODEL_DIR": "/model", "CLASSIFIER_THREADS": "1", "CLASSIFIER_MEM_ARENA": "false"})
    .add_local_file(ROOT / "src" / "backend_ai" / "gateway" / "understanding" / "onnx_inference.py", "/root/onnx_inference.py")
    .add_local_file(ROOT / "src" / "classifier_service" / "app.py", "/root/classifier_app.py")
)

app = modal.App("task-classifier", image=image)


@app.function(
    cpu=0.5,  # physical cores: 0.5 = 1 vCPU, what the service was measured on
    memory=1024,  # MiB; the service uses ~630-700
    secrets=[modal.Secret.from_name("classifier-token", required_keys=["CLASSIFIER_TOKEN"])],
    min_containers=MIN_CONTAINERS,
    max_containers=2,  # caps the bill if someone floods the URL
    scaledown_window=300,  # stay warm 5 minutes after the last request
    timeout=60,
)
@modal.concurrent(max_inputs=8)
@modal.asgi_app()
def web():
    from classifier_app import app as service

    return service
