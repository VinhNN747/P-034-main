# Shared backend code

This package is a library used by both Python processes, not an independently hosted service.

- `config.py`: common settings and repository-root `.env` discovery.
- `core`: FastAPI setup, JWT primitives, event loop and provider-independent pricing schemas.
- `database` / `models`: one database engine/session configuration and SQLAlchemy schema.
- `dependencies`: user authentication and permission helpers.
- `dtos`: contracts reused by management and AI, including their internal HTTP payloads.
- `repositories`: common key/auth/billing/audit/execution queries.
- `services`: the existing shared billing, execution persistence and runtime formatting helpers.

Do not import either service or AI libraries here. Before adding a shared helper, check that both services need it. Service-specific endpoints and orchestration belong to the owning service.

Both deployments keep the same Supabase `DATABASE_URL`. Alembic imports `src.shared.models` and `src.shared.database.base`; package relocation requires no migration.

See [source ownership](../README.md).
