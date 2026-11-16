import pytest

from src.backend_ai.gateway.aggregation.base import AggregationOutcome, BaseAggregator
from src.backend_ai.gateway.aggregation.concat_aggregator import ConcatAggregator
from src.backend_ai.gateway.aggregation.no_aggregator import NoAggregator
from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.pipeline.orchestrator import GatewayOrchestrator
from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.gateway.providers.offline import OfflineProvider
from src.shared.dtos.gateway import AggregationResult, AggregationStatus

pytest_plugins = ("tests.test_api.test_gateway_runtime",)


class FakeLLMAggregator(BaseAggregator):
    name = "llm"

    async def aggregate(self, request, decomposition, execution_results, constraints, goal_extraction=None):
        return AggregationOutcome(
            result=AggregationResult(
                original_request_id=request.request_id,
                status=AggregationStatus.COMPLETE,
                final_answer='{"calculation": 378, "translation": "AI Gateway is operating normally"}',
                contributions=[],
                strategy=self.name,
                reason="fake LLM synthesis",
            ),
            strategy=self.name,
            model="fake-aggregator-model",
        )


@pytest.mark.asyncio
async def test_exact_multitask_prompt_returns_final_answer_with_auto_aggregator(runtime_client, monkeypatch):
    client, application_id = runtime_client
    # The pool candidate must advertise the capabilities this prompt needs.
    from sqlalchemy import select

    from src.shared.database.session import get_db
    from src.shared.models.gateway import Model
    from tests.service_app import app
    async for session in app.dependency_overrides[get_db]():
        model = await session.scalar(select(Model))
        model.capabilities_json = ["chat", "text", "reasoning", "translation", "structured_output"]
        await session.commit()
        break
    from src.backend_ai.routers import gateway_runtime as runtime_module

    def build_fake_orchestrator(**kwargs):
        return GatewayOrchestrator(
            RequestPipeline(config=GatewayConfig(decomposition_mode="legacy")),
            routers={"database": kwargs["database_router"]},
            aggregators={
                "none": NoAggregator(),
                "deterministic": ConcatAggregator(),
                "llm": FakeLLMAggregator(),
            },
            providers={"offline": OfflineProvider()},
            persistence=kwargs["persistence"],
        )

    monkeypatch.setattr(runtime_module, "build_orchestrator", build_fake_orchestrator)
    response = await client.post(
        "/api/v1/gateway/requests",
        json={
            "application_id": application_id,
            "prompt": (
                "Thực hiện 2 task sau:\n\n"
                "Tính 27 × 14 và trả về kết quả.\n\n"
                'Dịch câu "AI Gateway đang hoạt động bình thường" sang tiếng Anh.\n\n'
                "Trả về kết quả dưới dạng JSON hợp lệ với 2 field: calculation và translation."
            ),
            "router": "deterministic",
            # Pinned: this is about the auto aggregator; the application's jev would run as the
            # one-task classifier in the offline test pipeline.
            "understander": "rule",
            "decomposer": "none",
            "aggregator": "auto",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["aggregation"]["status"] == "complete"
    assert body["aggregation"]["strategy"] == "llm"
    assert body["aggregation"]["final_answer"]
    assert body["request"]["metadata"]["aggregator_requested"] == "auto"
    assert body["request"]["metadata"]["aggregator_effective"] == "llm"
