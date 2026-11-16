from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.gateway.providers.embeddings import EmbeddingProviderResult
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.shared.dtos.runtime_api import RuntimeRequestResponse
from src.shared.models.runtime import GatewayRequestRecord, ModelExecutionRecord, UsageEventRecord
from src.shared.services.billing_service import BillingService


class EmbeddingPersistence:
    """Stage scalar usage/observability and billing for one embedding call."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def stage_run(
        self,
        principal: ApplicationPrincipal,
        request_id: str,
        model_id: str,
        provider_name: str,
        result: EmbeddingProviderResult | None,
        receipt: RuntimeRequestResponse,
        *,
        input_count: int,
        dimensions: int,
        input_price_per_million: Decimal,
        config_version: int,
        started_at: datetime,
        finished_at: datetime,
        error_code: str | None = None,
    ) -> GatewayRequestRecord:
        tokens = result.input_tokens if result else 0
        cost = Decimal(tokens) * input_price_per_million / Decimal(1_000_000)
        record = GatewayRequestRecord(
            request_id=request_id,
            application_id=principal.application_id,
            application_api_key_id=principal.key_id,
            config_version=config_version,
            prompt=None,
            messages_json=[],
            constraints_json={"dimensions": dimensions},
            source="openai_embeddings",
            strategies_json={"embedding_provider": provider_name},
            response_json={
                "request_id": request_id,
                "status": receipt.status,
                "model": model_id,
                "input_count": input_count,
                "dimensions": dimensions,
                "usage": receipt.usage.model_dump(mode="json") if receipt.usage else None,
                "cost": receipt.cost.model_dump(mode="json") if receipt.cost else None,
                "error_code": error_code,
            },
            status=receipt.status,
            aggregation_status=None,
            total_cost=float(cost),
            total_latency_ms=result.latency_ms if result else 0.0,
            started_at=started_at,
            finished_at=finished_at,
        )
        self.session.add(record)
        await self.session.flush()

        execution = ModelExecutionRecord(
            request_id=request_id,
            task_id="embedding",
            provider=provider_name,
            model=model_id,
            selected_model=model_id,
            status="success" if result else "failed",
            fallback=False,
            failure_type=None if result else error_code,
            failure_reason=None,
            input_tokens=tokens,
            output_tokens=0,
            cost_usd=float(cost),
            latency_ms=result.latency_ms if result else 0.0,
            result_payload=None,
        )
        self.session.add(execution)
        if result is not None:
            usage = UsageEventRecord(
                request_id=request_id,
                application_id=principal.application_id,
                provider=provider_name,
                model=model_id,
                input_tokens=result.input_tokens,
                output_tokens=0,
                cost_usd=float(cost),
                estimated=True,
                source="openai_embeddings",
            )
            self.session.add(usage)
        transaction = await BillingService(self.session).debit_runtime_request(
            principal.application_id,
            request_id,
            cost,
            receipt.status,
        )
        if transaction is not None:
            record.billing_transaction_id = transaction.id
            if result is not None:
                usage.billing_transaction_id = transaction.id
        return record
