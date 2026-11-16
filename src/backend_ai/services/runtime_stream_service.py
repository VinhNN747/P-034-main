"""Bounded event transport; the existing runtime remains the execution owner."""

import asyncio
from contextvars import ContextVar

import anyio

from src.backend_ai.gateway.providers.base import ProviderStreamEvent
from src.shared.dtos.runtime_api import RuntimeRequestResponse

_current: ContextVar["RuntimeStreamSession | None"] = ContextVar("openai_runtime_stream", default=None)


def current_stream():
    return _current.get()


class RuntimeStreamSession:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=64)
        self.published = 0
        self.request_id = None
        self.model = None
        self.closed = False
        self.task = None
        self._first = None
        self.partial_result = None
        self.pricing = None
        self.output_order = []
        self._tool_headers = {}

    def record_usage(self, response, *, task_id, model_id=None, cost=None, estimated=None):
        """Keep measured counters independently of the unfinished output."""
        usage = response.metadata.get('anthropic_usage') if isinstance(response.metadata, dict) else None
        has_observed_usage = response.usage_received or (
            isinstance(usage, dict) and any(type(value) is int and value >= 0 for value in usage.values())
        )
        if self.partial_result is None or not has_observed_usage:
            return
        from src.backend_ai.gateway.observability.cost_tracker import summarize_costs
        from src.shared.dtos.gateway import ExecutionResultTrace, StageTrace
        model_id = model_id or response.model_id
        if cost is None:
            cost, estimated = self.pricing.calculate_usage(
                model_id, response.input_tokens, response.output_tokens, response.output_evidence
            )
        estimated = bool(estimated) or not response.usage_received
        result = self.partial_result
        stage = 'aggregate' if task_id == 'aggregate' else 'route_execute:' + task_id
        result.traces = [trace for trace in result.traces if trace.stage != stage]
        result.traces.append(StageTrace(request_id=result.request.request_id, stage=stage,
            strategy='stream_observed_usage', model=model_id, status='error',
            input_tokens=response.input_tokens, output_tokens=response.output_tokens, cost_usd=cost,
            metadata={'usage_complete':False,'cost_estimated':bool(estimated),
                      'output_evidence':response.output_evidence.model_dump(mode='json')}))
        if task_id != 'aggregate':
            result.execution_results = [item for item in result.execution_results or [] if item.task_id != task_id]
            result.execution_results.append(ExecutionResultTrace(task_id=task_id, status='failed',
                executed_model=model_id, selected_model=model_id, provider=response.provider,
                input_tokens=response.input_tokens, output_tokens=response.output_tokens, cost_usd=cost,
                cost_estimated=bool(estimated), usage_complete=False, output_evidence=response.output_evidence))
        result.costs = summarize_costs(result.traces)

    def start(self, owner):
        if self.task is not None:
            raise RuntimeError("Stream already has an execution owner")

        async def run():
            token = _current.set(self)
            try:
                return await owner()
            finally:
                _current.reset(token)
                if not self.closed:
                    await self.queue.put(None)

        self.task = asyncio.create_task(run())
        return self

    async def emit(self, event: ProviderStreamEvent):
        if event.type in {"text_delta", "tool_delta", "refusal_delta"}:
            key = 'message'
            if event.type == 'tool_delta':
                delta = event.tool_delta
                header = self._tool_headers.setdefault(delta['index'], {'id':'', 'name':''})
                header['id'] += delta.get('id') or ''
                header['name'] += (delta.get('function') or {}).get('name') or ''
                key = 'function_call:' + str(delta['index']) if header['id'] and header['name'] else None
            if key is not None and key not in self.output_order:
                self.output_order.append(key)
            self.published += 1
            await self.queue.put(event)

    async def prime(self):
        """Pre-provider errors are still HTTP errors, before SSE headers."""
        self._first = await self.queue.get()
        if self._first is None:
            return await self.final_response()
        return None

    async def events(self):
        try:
            if self._first is not None:
                yield self._first
                self._first = None
            while True:
                event = await self.queue.get()
                if event is None:
                    break
                yield event
        finally:
            if self.task is not None and not self.task.done():
                await self.aclose()

    async def final_response(self):
        return await asyncio.shield(self.task)

    async def final_receipt(self):
        response = await self.final_response()
        return RuntimeRequestResponse.model_validate_json(response.body)

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            # Starlette cancels the consumer's AnyIO scope on disconnect. Keep
            # the request dependency/session alive until the owner records its
            # terminal state, and never cancel that owner a second time.
            with anyio.CancelScope(shield=True):
                try:
                    await asyncio.wait_for(asyncio.shield(self.task), timeout=10)
                except asyncio.CancelledError:
                    pass
