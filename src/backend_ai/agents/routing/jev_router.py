"""BaseRouter implementation that wraps the existing, fully-tested
SubtaskRouter (jev/ModelRouterMiddleware) by composition rather than
refactoring it in place. SubtaskRouter's internals (17 tests in
test_routing.py, including the jev-vs-model-call error taxonomy fix
documented in errors.py) are left untouched; JevRouter only adapts its
output to the BaseRouter/RouteDecision interface.
"""

from __future__ import annotations

from src.backend_ai.agents.routing.base import BaseRouter, RouteDecision
from src.backend_ai.agents.routing.router import ExecutionConfig, SubtaskRouter
from src.shared.config import Settings
from src.shared.dtos.gateway import SubTask


class JevRouter(BaseRouter):
    """Routes AND executes in one step via jev (see router.py's module
    docstring for why route+execute can't be split for this strategy)."""

    name = "jev"

    def __init__(
        self,
        subtask_router: SubtaskRouter | None = None,
        execution_config: ExecutionConfig | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._subtask_router = subtask_router or SubtaskRouter(execution_config=execution_config, settings=settings)

    async def aroute(
        self,
        subtask: SubTask,
        upstream_results: dict[str, str] | None = None,
        *,
        original_prompt: str | None = None,
        request_context: dict[str, object] | None = None,
        route_hints: dict[str, object] | None = None,
        **kwargs: object,
    ) -> RouteDecision:
        result = await self._subtask_router.aexecute_subtask(
            subtask,
            upstream_results,
            original_prompt=original_prompt,
            request_context=request_context,
        )
        model_names = getattr(self._subtask_router, "_model_names", {})
        tier = next(
            (tier for tier, name in model_names.items() if name == result.selected_model),
            result.route,
        )
        return RouteDecision(
            task_id=subtask.task_id,
            tier=tier or ("fallback" if result.fallback else "unknown"),
            strategy=self.name,
            selected_model=result.executed_model or result.selected_model,
            confidence=result.confidence,
            prefetched_result=result,
        )
