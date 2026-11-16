# AI backend

Start with `uvicorn src.backend_ai.main:app --port 8001` from the repository root. Install `src/requirements-ai.txt`.

This package owns chat/agent execution, analyze/playground execution, OpenAI-compatible APIs, native runtime requests, application-key authentication and authenticated internal provider operations.

- `agents`: graph and model routing.
- `gateway`: understanding, validation, scoring, decomposition, execution providers, aggregation and tracing.
- `services`: DB-backed execution/routing, runtime idempotency and responses, support synthesis.
- `routers`, `dependencies`, `repositories`, `dtos`: AI-specific HTTP and persistence layers.

Add future local LLM or RAG execution here. Reuse `src.shared`; do not import management implementation. Runtime continues to write receipt/usage/billing to the existing shared DB transaction.

Python clients use the official OpenAI SDK against the `/v1` routers. Native runtime receipts remain available through `routers/runtime.py`.

The comparison queue starts automatically with the AI application lifespan (`COMPARISON_WORKER_ENABLED=true`). No separate terminal is needed. Shutdown drains the current run up to `COMPARISON_WORKER_SHUTDOWN_SECONDS`, then marks interrupted work indeterminate without replaying provider calls. Disable the embedded worker only when deploying the optional standalone worker.

Adaptive provider-attempt latency collection, prediction and fitting also run inside the AI backend lifespan when `GATEWAY_LATENCY_MODE=auto`. Training runs on the AI backend's scheduler and its CPU fit runs in a supervised child process; it does not require a third backend. `auto` is the default and each candidate still needs to pass the serving gates before its prediction affects routing. Use `off` to disable latency collection, training and prediction. Configuration, reporting and rollback scripts are in `src/scripts/`.

In live mode this service and any standalone comparison worker must receive the
same unique `CREDENTIAL_ENCRYPTION_KEY` as the management backend. Provider and
JEV keys resolve from encrypted DB credentials; do not configure upstream keys
in `.env`.

Playground supports routing first, then choosing reference models and an independent evaluator from the application's selected pool, or the active catalog when no pool is selected. `gateway_request_id` reuses the persisted routing receipt, context and constraints without another Gateway execution or debit. Comparisons default to `execution_strategy="sequential"`; `"parallel"` is optional. Quality scoring runs after reference outputs are ready.

See [source ownership](../README.md) and [run guide](../../README.md).
