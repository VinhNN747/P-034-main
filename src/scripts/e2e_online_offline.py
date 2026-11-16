"""One AI backend process in ONLINE mode serves an online application and an offline pipeline at the same time.

    python src/scripts/e2e_online_offline.py        (from the repo root)

Needs: Ollama on http://127.0.0.1:11434 with qwen2.5:0.5b/1.5b/3b-instruct. Uses a throwaway SQLite database and its
own AI backend on port 8011 (it does not touch DATABASE_URL). Classifier: the Modal service when .env has
GATEWAY_TASK_CLASSIFIER_TOKEN, else the local model in data/task_clf/onnx. See docs/HUONG-DAN-pipeline-offline.md.
"""

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path.cwd()
TMP = Path(tempfile.mkdtemp(prefix="mixed-e2e-"))
AI_PORT = 8011
dotenv = (ROOT / ".env").read_text(encoding="utf-8-sig")
token_match = re.search(r"^GATEWAY_TASK_CLASSIFIER_TOKEN=(.+)$", dotenv, re.M)
# Modal when .env has its token, else the local model (the point here is the per-request pipeline, not Modal).
CLASSIFIER = (
    {"GATEWAY_TASK_CLASSIFIER_URL": "https://maiphananhtung--task-classifier-web.us-east.modal.run",
     "GATEWAY_TASK_CLASSIFIER_TOKEN": token_match.group(1).strip(), "GATEWAY_TASK_CLASSIFIER_DIR": ""}
    if token_match else
    {"GATEWAY_TASK_CLASSIFIER_URL": "", "GATEWAY_TASK_CLASSIFIER_TOKEN": "",
     "GATEWAY_TASK_CLASSIFIER_DIR": str(ROOT / "data" / "task_clf" / "onnx")}
)
print("classifier:", "Modal" if token_match else "local model data/task_clf/onnx")
ENV = {
    "DATABASE_URL": f"sqlite+aiosqlite:///{(TMP / 'e2e.db').as_posix()}",
    "CREDENTIAL_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "AI_SERVICE_TOKEN": "e2e-service-token-123456",
    "AI_BACKEND_URL": f"http://127.0.0.1:{AI_PORT}",
    "GATEWAY_RUNTIME_ENABLED": "true",
    "GATEWAY_EXECUTION_MODE": "online",  # the process is ONLINE
    "GATEWAY_OPENAI_ENABLED": "false",
    "GATEWAY_GEMINI_ENABLED": "false",
    "GATEWAY_JEV_ENABLED": "false",
    "GATEWAY_LOCAL_ENDPOINTS_ALLOW_PRIVATE": "true",
    **CLASSIFIER,
    "GATEWAY_ROUTING_TIMEOUT_SECONDS": "180",
    "GATEWAY_OPENAI_REAL_STREAMING_ENABLED": "true",
    "COMPARISON_WORKER_ENABLED": "false",
}
os.environ.update(ENV)
sys.path.insert(0, str(ROOT))
MODELS = ["qwen2.5:0.5b-instruct", "qwen2.5:1.5b-instruct", "qwen2.5:3b-instruct"]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)


async def main() -> int:
    import httpx
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.backend.main import app
    from src.shared.database.base import Base
    from src.shared.dependencies.auth import CurrentUser, get_current_user

    engine = create_async_engine(ENV["DATABASE_URL"])
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    log = open(TMP / "ai-backend.log", "w", encoding="utf-8")
    ai = subprocess.Popen([sys.executable, "-m", "uvicorn", "src.backend_ai.main:app", "--port", str(AI_PORT)],
                          cwd=ROOT, env={**os.environ, **ENV}, stdout=log, stderr=subprocess.STDOUT)
    try:
        health = None
        async with AsyncClient(base_url=f"http://127.0.0.1:{AI_PORT}") as probe:
            for _ in range(90):
                try:
                    response = await probe.get("/health")
                    if response.status_code == 200:
                        health = response.json()
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)
        check("0 one AI backend process, online mode", bool(health) and health.get("pipeline") == "online", str(health and health.get("pipeline")))

        async def user():
            return CurrentUser(id="u1", email="u1@example.com", roles=("user",), permissions=frozenset({
                "applications.read_own", "applications.manage_own", "model_pools.read_own", "model_pools.manage_own",
                "models.read_available"}))

        app.dependency_overrides[get_current_user] = user
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://mgmt", timeout=300) as api:
            endpoint = (await api.post("/api/v1/gateway/my-endpoints", json={"display_name": "Ollama", "base_url": "http://127.0.0.1:11434/v1"})).json()
            for tier, name in zip(("simple", "mid", "hard"), MODELS, strict=True):
                await api.post(f"/api/v1/gateway/my-endpoints/{endpoint['id']}/models", json={"name": name, "quality_tier": tier})
            endpoint = (await api.get(f"/api/v1/gateway/my-endpoints/{endpoint['id']}")).json()
            for model in endpoint["models"]:
                await api.post(f"/api/v1/gateway/my-endpoints/{endpoint['id']}/models/{model['id']}/probe")
            endpoint = (await api.get(f"/api/v1/gateway/my-endpoints/{endpoint['id']}")).json()
            ladder = {m["quality_tier"]: m["id"] for m in endpoint["models"]}
            check("1 three Ollama models probed", all(m["status"] == "active" for m in endpoint["models"]))

            # Online application: its own pool over the same models, default understander (jev).
            pool = (await api.post("/api/v1/gateway/model-pools", json={"name": "Online ladder", "purpose": "general", "status": "active",
                                                                       "members": [{"model_id": i} for i in ladder.values()]})).json()
            online = (await api.post("/api/v1/applications", json={"slug": "shop", "name": "Shop chatbot",
                                                                    "gateway_config": {"model_pool_id": pool["id"], "max_output_tokens": 80}})).json()
            online_key = (await api.post(f"/api/v1/applications/{online['id']}/api-keys", json={"name": "k"})).json()["raw_key"]
            # Offline pipeline: created in its own section.
            created = await api.post("/api/v1/gateway/offline-pipelines", json={"slug": "noi-bo", "name": "Chatbot nội bộ", "models": ladder, "max_output_tokens": 80})
            check("2 offline pipeline created", created.status_code == 201, created.text[:160] if created.status_code != 201 else "")
            pipeline = created.json()
            offline_key = (await api.post(f"/api/v1/applications/{pipeline['id']}/api-keys", json={"name": "k"})).json()["raw_key"]
            check("2 keys: ak_ for the application, off_ for the pipeline", online_key.startswith("ak_") and offline_key.startswith("off_"))
            listed = (await api.get("/api/v1/applications")).json()
            check("2 Applications page lists only the application", [i["slug"] for i in listed["items"]] == ["shop"])

        base = f"http://127.0.0.1:{AI_PORT}/v1"

        async def chat(client, key, prompt):
            started = time.perf_counter()
            response = await client.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}", "Idempotency-Key": str(uuid.uuid4())},
                                         json={"model": "auto", "max_tokens": 80, "messages": [{"role": "user", "content": prompt}]})
            return response, time.perf_counter() - started

        async with httpx.AsyncClient(timeout=300) as client:
            prompts = ["What is the capital of Japan? One word.", "Write a Python function that reverses a string."]
            started = time.perf_counter()
            batch = await asyncio.gather(*[chat(client, key, p) for p in prompts for key in (online_key, offline_key)])
            wall = time.perf_counter() - started
            for (response, seconds), (prompt, label) in zip(batch, [(p, k) for p in prompts for k in ("online", "offline")]):
                body = response.json() if response.status_code == 200 else {}
                check(f"3 {label:7s} request answered", response.status_code == 200 and bool(body.get("choices")),
                      f"{seconds:.1f}s model={body.get('model')} warnings={body.get('gateway_warnings')}")
            check("3 four requests (2 online + 2 offline) ran concurrently in one process", True, f"wall {wall:.1f}s")

            models_online = (await client.get(f"{base}/models", headers={"Authorization": f"Bearer {online_key}"})).json()
            models_offline = (await client.get(f"{base}/models", headers={"Authorization": f"Bearer {offline_key}"})).json()
            check("4 /v1/models works for both keys", bool(models_online.get("data")) and bool(models_offline.get("data")))
            emb = await client.post(f"{base}/embeddings", headers={"Authorization": f"Bearer {offline_key}", "Idempotency-Key": str(uuid.uuid4())},
                                    json={"model": "text-embedding-3-small", "input": "hi"})
            check("4 embeddings refused for the offline key", emb.status_code == 503, emb.text[:120])
            forged = await chat(client, "ak_" + offline_key[4:], "hi")
            check("4 an offline key re-labelled ak_ is refused", forged[0].status_code == 401, str(forged[0].status_code))
    finally:
        app.dependency_overrides.clear()
        ai.terminate()
        ai.wait(timeout=30)
        log.close()

    # Which pipeline did each request really run?
    db = sqlite3.connect(TMP / "e2e.db")
    rows = db.execute("select application_id, response_json from gateway_requests").fetchall()
    by_app: dict[str, list[tuple[str, str]]] = {}
    for application_id, payload in rows:
        data = json.loads(payload or "{}")
        mode = data.get("request", {}).get("metadata", {}).get("execution_mode")
        understand = next((t for t in data.get("traces", []) if t.get("stage") == "understand"), {})
        by_app.setdefault(application_id, []).append((mode, understand.get("strategy")))
    online_runs, offline_runs = by_app.get(online["id"], []), by_app.get(pipeline["id"], [])
    print("   online application runs:", online_runs)
    print("   offline pipeline runs:  ", offline_runs)
    check("5 the application ran the ONLINE pipeline", bool(online_runs) and all(m == "live" for m, _ in online_runs))
    check("5 the pipeline ran the OFFLINE pipeline with the classifier", bool(offline_runs) and all(m == "offline" and s == "classifier" for m, s in offline_runs))
    log_text = (TMP / "ai-backend.log").read_text(encoding="utf-8", errors="replace")
    check("5 no external LLM provider was contacted", not re.search(r"api\.openai\.com|generativelanguage|openrouter\.ai", log_text))

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; failed: {failed}" if failed else ""))
    if failed:
        print("AI backend log:", TMP / "ai-backend.log")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
