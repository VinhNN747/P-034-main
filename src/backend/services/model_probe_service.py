from urllib.parse import quote

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.clients.ai_backend_client import AIBackendClient, AIBackendUnavailableError, get_ai_backend_client
from src.backend.services.catalog_service import CatalogReferenceError
from src.backend.services.model_catalog_service import ModelCatalogService
from src.shared.dtos.ai_internal import ModelProbeTarget
from src.shared.dtos.model_catalog import ModelProbeRead


class ModelProbeService:
    def __init__(self, session: AsyncSession, ai_client: AIBackendClient | None = None):
        self.session = session
        self.catalog = ModelCatalogService(session)
        self.ai_client = ai_client or get_ai_backend_client()

    async def probe(self, model_id: str) -> ModelProbeRead:
        pair = await self.catalog.get(model_id)
        if pair is None:
            raise CatalogReferenceError("Model not found")
        model, _ = pair
        payload = ModelProbeTarget()
        body = await self.ai_client.post(
            f"/internal/v1/models/{quote(model_id, safe='')}/probe", payload.model_dump(mode="json")
        )
        try:
            result = ModelProbeRead.model_validate(body)
            if result.model_id != model_id or result.status not in {"healthy", "unhealthy", "unavailable", "offline"}:
                raise ValueError("Invalid probe result")
        except (ValidationError, ValueError) as exc:
            raise AIBackendUnavailableError() from exc
        model.health_status = result.status
        model.last_probe_at = result.checked_at
        model.last_probe_latency_ms = result.latency_ms
        model.last_probe_error = result.failure_reason
        model.last_probe_credential_version = result.credential_version
        await self.session.commit()
        return result
