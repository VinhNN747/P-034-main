from dataclasses import dataclass, field

import pytest

from src.backend_ai.agents.routing.deterministic_router import DeterministicRouter
from src.backend_ai.gateway.aggregation.no_aggregator import NoAggregator
from src.backend_ai.gateway.pipeline.orchestrator import GatewayOrchestrator
from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.gateway.providers.base import ProviderClient, ProviderResponse
from src.backend_ai.gateway.providers.offline import OfflineProvider
from src.shared.dtos.gateway import GatewayRequest


@dataclass
class RecordingPersistence:
    calls: list[tuple] = field(default_factory=list)

    async def save_run(self, result, execution_results=None):
        self.calls.append((result, execution_results))


@pytest.mark.asyncio
async def test_orchestrator_persists_terminal_offline_run():
    persistence = RecordingPersistence()
    orchestrator = GatewayOrchestrator(
        RequestPipeline(),
        routers={"deterministic": DeterministicRouter(model_by_tier={"small_fast": "offline"})},
        aggregators={"none": NoAggregator()},
        persistence=persistence,
        providers={"offline": OfflineProvider()},
    )

    result = await orchestrator.run(
        GatewayRequest(request_id="req-orchestrator", application_id="app-1", prompt="hello"),
        understander="rule",
        decomposer="none",
        router="deterministic",
        aggregator="none",
    )

    assert result.aggregation is not None
    assert result.aggregation.status == "complete"
    assert len(persistence.calls) == 1
    assert persistence.calls[0][0].request.request_id == "req-orchestrator"


@dataclass
class RecordingProvider(ProviderClient):
    name: str = "recording"

    async def generate(self, prompt, *, task_id, model_id, timeout_seconds):
        return ProviderResponse(
            text=f"recording:{model_id}:{task_id}",
            provider=self.name,
            model_id=model_id,
        )


@pytest.mark.asyncio
async def test_orchestrator_passes_injected_provider_map_to_executor():
    provider = RecordingProvider()
    orchestrator = GatewayOrchestrator(
        RequestPipeline(),
        routers={"deterministic": DeterministicRouter(model_by_tier={"small_fast": "recording-model"})},
        aggregators={"none": NoAggregator()},
        providers={"recording-model": provider},
    )

    result = await orchestrator.run(
        GatewayRequest(request_id="req-provider-map", application_id="app-1", prompt="hello"),
        understander="rule",
        decomposer="none",
        router="deterministic",
        aggregator="none",
    )

    assert result.execution_results[0].executed_model == "recording-model"
    assert result.execution_results[0].result == "recording:recording-model:task-1"
