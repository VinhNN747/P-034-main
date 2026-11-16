-- The schema of migrations 20261014_customer_endpoints + 20261015_offline_pipelines, for a database whose alembic
-- chain cannot run yet (see docs/migration-customer-endpoints.md). Safe to run twice. It does NOT move
-- alembic_version: once the chain is joined, mark both as done with `alembic stamp 20261015_offline_pipelines`.
ALTER TABLE providers ADD COLUMN IF NOT EXISTS owner_user_id VARCHAR(100);
CREATE INDEX IF NOT EXISTS ix_providers_owner_user_id ON providers (owner_user_id);
ALTER TABLE applications ADD COLUMN IF NOT EXISTS pipeline VARCHAR(20) NOT NULL DEFAULT 'online';
ALTER TABLE application_gateway_configs ADD COLUMN IF NOT EXISTS classifier_mid_difficulty FLOAT;
ALTER TABLE application_gateway_configs ADD COLUMN IF NOT EXISTS classifier_large_difficulty FLOAT;
