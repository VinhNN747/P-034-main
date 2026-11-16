# Management backend

Start with `uvicorn src.backend.main:app --port 8000` from the repository root. Install `src/requirements-backend.txt`.

This package owns applications, API-key administration, catalog/configuration, billing administration, reporting/incidents, policy reviews and support conversations.

- `routers`: HTTP endpoints and permission checks.
- `services`: management use cases.
- `clients`: authenticated calls to the AI service for support synthesis and model probes.
- `repositories`: management-only persistence queries.
- `dtos`: management-only contracts.

Reuse `src.shared` for models, database, user authentication and shared persistence. Do not import `src.backend_ai` or model runtime libraries.

The support chat endpoint stays here; only model synthesis runs on AI. Model probe permissions and storage stay here; provider invocation runs on AI.

In live mode this service must receive the same `CREDENTIAL_ENCRYPTION_KEY` as
the AI backend and comparison worker. Admins manage provider keys through
`/providers`; they are encrypted in the shared DB. Do not configure upstream
provider keys in `.env`.

See [source ownership](../README.md) and [run guide](../../README.md).
