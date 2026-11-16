from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def _run_revision(connection, operation: str, revision: str) -> None:
    config = Config("src/alembic.ini")
    config.attributes["connection"] = connection
    getattr(command, operation)(config, revision)


def test_anthropic_feature_migration_is_nullable_and_preserves_existing_catalog(tmp_path: Path):
    database_path = tmp_path / "anthropic-model-features.db"
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            _run_revision(connection, "upgrade", "20261015_offline_pipelines")
            with connection.begin():
                connection.execute(text(
                    "INSERT INTO providers (id,name,display_name,kind,status,purpose,secret_env_var,created_at,updated_at) "
                    "VALUES ('provider-1','legacy','Legacy','openai','active','target','',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                ))
                connection.execute(text(
                    "INSERT INTO models (id,provider_id,name,display_name,status,capabilities_json,"
                    "input_price_per_million,output_price_per_million,pricing_version,created_at,updated_at) "
                    "VALUES ('model-1','provider-1','legacy-model','Legacy Model','active','[]',1,2,'old',"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                ))

            _run_revision(connection, "upgrade", "head")
            model_columns = {column["name"] for column in inspect(connection).get_columns("models")}
            assert {"max_output_tokens", "feature_metadata_json"} <= model_columns
            assert connection.execute(text(
                "SELECT name,max_output_tokens,feature_metadata_json FROM models WHERE id='model-1'"
            )).one() == ("legacy-model", None, None)

            _run_revision(connection, "downgrade", "20261015_offline_pipelines")
            model_columns = {column["name"] for column in inspect(connection).get_columns("models")}
            assert "max_output_tokens" not in model_columns
            assert connection.scalar(text("SELECT name FROM models WHERE id='model-1'")) == "legacy-model"
    finally:
        engine.dispose()
