# syntax=docker/dockerfile:1
# The task classifier service for Cloud Run, model baked in. Build from the repo root:
#   docker build -f src/classifier_service/Dockerfile --secret id=hf_token,src=<file holding a read-only HF token> \
#     --build-arg MODEL_URL=https://huggingface.co/<user>/<repo>/resolve/<commit> -t task-classifier .
# (Cloud Build: src/classifier_service/cloudbuild.yaml.) The token is a build secret: it is not stored in any layer.

# ---- Stage 1: download and verify the model ----
FROM python:3.11-slim AS model
ARG MODEL_URL
RUN pip install --no-cache-dir httpx==0.28.1
COPY src/scripts/fetch_task_classifier.py /build/scripts/
RUN --mount=type=secret,id=hf_token,required=true \
    test -n "$MODEL_URL" && python /build/scripts/fetch_task_classifier.py \
      --dir /model --source-url "$MODEL_URL" --token-file /run/secrets/hf_token

# ---- Stage 2: runtime ----
FROM python:3.11-slim
WORKDIR /app
COPY src/classifier_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -m appuser
COPY --from=model /model /model
COPY src/backend_ai/gateway/understanding/onnx_inference.py src/classifier_service/app.py ./
USER appuser
ENV CLASSIFIER_MODEL_DIR=/model PYTHONUNBUFFERED=1
# Cloud Run sets PORT (8080).
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --no-access-log"]
