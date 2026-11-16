# Source ownership

The source is organized by deployed service first, then by layer. Both services share the existing database; moving packages does not require a migration.

```text
src/
├── backend/                    Management API, port 8000
│   ├── main.py
│   ├── routers/                Applications, keys, catalog/config, billing, reports, support
│   ├── services/               Management business logic
│   ├── clients/                Authenticated HTTP calls to AI
│   ├── repositories/           Management-only application/catalog queries
│   └── dtos/                   Management-only request/response contracts
├── backend_ai/                 AI execution and OpenAI-compatible API, port 8001
│   ├── main.py
│   ├── routers/                Chat, analyze, native runtime, OpenAI-compatible API, internal endpoints
│   ├── services/               Execution, routing, idempotency, responses, synthesis
│   ├── dependencies/           Application-key and internal-service authentication
│   ├── repositories/           Runtime operation queries
│   ├── dtos/                   AI-only request/response contracts
│   ├── agents/                 Agent graph and model-routing algorithms
│   └── gateway/                Pipeline, understanding, decomposition, providers, aggregation
└── shared/                     Reused code; no dependency on either service
    ├── config.py               Settings; loads repository-root .env
    ├── core/                   HTTP app setup, JWT security, event loop, pricing
    ├── database/               ORM base, engine and sessions
    ├── models/                 One SQLAlchemy entity registry
    ├── dependencies/           User auth and permission helpers
    ├── dtos/                   Common API/pipeline/internal-service contracts
    ├── repositories/           Keys, authorization, audit, billing, gateway persistence
    └── services/               Existing shared billing/persistence/runtime helpers
```

The remaining runtime assets are also under `src/`: `frontend/` (Next.js),
`classifier_service/` (standalone task classifier), `scripts/` (local and
operational commands), `alembic/` and `alembic.ini` (database migrations),
`deploy/` (Nginx and Supabase templates), `Dockerfile`, and
`docker-compose.yml`. Run commands from the repository root; see the
[local run guide](../README.md).

## Dependency rules

- `backend` may import `backend` and `shared`.
- `backend_ai` may import `backend_ai` and `shared`.
- `shared` may import only `shared` within `src`; it cannot import either service.
- Management and shared cannot import model runtime libraries such as LangChain or LangGraph.
- Management calls provider operations through `backend/clients/ai_backend_client.py`, not by importing AI implementation.

`tests/test_package_boundaries.py` checks imports, including relative imports and literal dynamic imports. `tests/test_api/test_split_backends.py` checks route ownership and management startup without AI libraries.

## Shared services

| File | Why both services use it |
|---|---|
| `shared/services/billing_service.py` | Management administers billing; AI records execution charges |
| `shared/services/gateway_persistence_service.py` | AI writes execution/usage; management reads history |
| `shared/services/gateway_runtime_service.py` | Existing runtime catalog/config and result/history formatting |

Their method bodies and transaction boundaries remain unchanged. Receipt, usage and billing writes stay in the runtime transaction. Shared code runs locally in each process; `shared` is not a third HTTP service.

Add new management features beneath `backend`, and new routing/LLM/RAG features beneath `backend_ai`. Put code in `shared` only when both services need it and it has no dependency on either service.

## Entry points

```bash
uvicorn src.backend.main:app --reload --port 8000
uvicorn src.backend_ai.main:app --reload --port 8001
```

Python clients use the official OpenAI SDK against the AI service's `/v1` endpoints. The frontend is in `src/frontend`. Public endpoint paths and ports are unchanged; the old flat entrypoints have been replaced.

From the repository root, the moved Makefile can be used with
`make -f src/Makefile run` or `make -f src/Makefile run-ai`.

See [README.md](../README.md) for dependency setup and hosting.
