"""The task classifier as a small HTTP service (Cloud Run): POST /classify -> task type + difficulty.

The gateway calls it with GATEWAY_TASK_CLASSIFIER_URL / _TOKEN set (RemoteTaskClassifier), so the
gateway host needs no model RAM. The model is baked into the image (see Dockerfile), so a cold
start only loads it from local disk.

Environment:
  CLASSIFIER_TOKEN      required; callers send "Authorization: Bearer <token>"
  CLASSIFIER_MODEL_DIR  default /model
  CLASSIFIER_THREADS    default 1 (one Cloud Run vCPU)
  CLASSIFIER_MEM_ARENA  default false (fits 2 GiB)
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from onnx_inference import OnnxClassifierModel
from pydantic import BaseModel, Field

# The model reads at most max_len (256) tokens; longer text only costs tokenizer time.
MAX_TEXT_CHARS = 20_000


class ClassifyRequest(BaseModel):
    text: str = Field(max_length=MAX_TEXT_CHARS)


class ClassifyResponse(BaseModel):
    task_type: str
    probability: float
    difficulty: float
    probabilities: dict[str, float]
    model: str
    latency_ms: float


def _env_flag(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    token = os.environ.get("CLASSIFIER_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("CLASSIFIER_TOKEN must be set to a random secret of at least 32 characters")
    model_dir = Path(os.environ.get("CLASSIFIER_MODEL_DIR", "/model"))
    missing = OnnxClassifierModel.missing_files(model_dir)
    if missing:
        raise RuntimeError(f"{', '.join(missing)} missing in {model_dir}")
    app.state.token = token
    app.state.model = OnnxClassifierModel(
        model_dir,
        threads=int(os.environ.get("CLASSIFIER_THREADS", "1")),
        mem_arena=_env_flag("CLASSIFIER_MEM_ARENA", False),
    )
    app.state.model.predict("warm up")
    yield


app = FastAPI(title="Task classifier", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def require_token(authorization: str = Header(default="")) -> None:
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), app.state.token.encode()):
        raise HTTPException(status_code=401, detail="invalid token")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "model": app.state.model.name}


@app.post("/classify", response_model=ClassifyResponse, dependencies=[Depends(require_token)])
def classify(body: ClassifyRequest) -> ClassifyResponse:
    # A sync endpoint: FastAPI runs it in its thread pool, so concurrent requests share the session.
    prediction = app.state.model.predict(body.text)
    return ClassifyResponse(
        task_type=prediction.task,
        probability=prediction.probability,
        difficulty=prediction.difficulty,
        probabilities=prediction.probabilities,
        model=app.state.model.name,
        latency_ms=round(prediction.latency_ms, 1),
    )
