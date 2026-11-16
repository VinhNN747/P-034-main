from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from src.shared.dtos.gateway import StageTrace


class TraceCollector:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.traces: list[StageTrace] = []

    def add(
        self,
        *,
        stage: str,
        strategy: str,
        started_at: datetime,
        timer_started: float,
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        status: str = "success",
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StageTrace:
        trace = StageTrace(
            request_id=self.request_id,
            stage=stage,
            strategy=strategy,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=round((perf_counter() - timer_started) * 1000, 3),
            status=status,  # type: ignore[arg-type]
            error=error,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            metadata=metadata or {},
        )
        self.traces.append(trace)
        return trace


def trace_timer() -> tuple[datetime, float]:
    return datetime.now(UTC), perf_counter()


def execution_trace_rows_to_stage_traces(request_id: str, rows: list[dict[str, Any]]) -> list[StageTrace]:
    """Converts ``src.backend_ai.agents.routing.router.execution_trace_rows()`` output
    (one row per subtask, ``stage="route_execute:<task_id>"``) into real
    ``StageTrace`` rows so route+execute shows up in ``PipelineResult.traces``
    /``stage_trace_rows()`` alongside every other pipeline stage, instead of
    living in a separate table. ``latency_ms`` is passed through as-is
    (already measured by the router/executor) rather than re-timed here."""
    # ExecutionResult.status is "success" | "failed"; StageTrace.status is the
    # pipeline-wide vocabulary "success" | "error" | "fallback" | "skipped" —
    # "failed" has no direct match there, so it maps to "error" (a fallback
    # that still ended in success keeps status="success" with error set,
    # matching how every other stage reports a recovered failure).
    status_map = {"success": "success", "failed": "error"}
    traces: list[StageTrace] = []
    for row in rows:
        strategy, _, model = str(row["strategy/model"]).partition(" / ")
        traces.append(
            StageTrace(
                request_id=request_id,
                stage=row["stage"],
                strategy=strategy,
                model=model if model and model != "-" else None,
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cost_usd=row["cost_usd"],
                latency_ms=row["latency_ms"],
                status=status_map.get(row["status"], "error"),  # type: ignore[arg-type]
                error=row["error"],
                # Router's pricing table (_DEFAULT_MODEL_PRICING in router.py) is always
                # estimated=True (hardcoded reference prices), so every route_execute row is too.
                metadata={
                    "cost_estimated": True,
                    "usage_complete": bool(row.get("usage_complete", False)),
                    "route": row.get("route"),
                    "route_reason": row.get("route_reason"),
                    "route_shadow": row.get("route_shadow"),
                    "circuit_skipped": row.get("circuit_skipped", []),
                    "output_evidence": row.get("output_evidence"),
                },
            )
        )
    return traces


def stage_trace_rows(traces: list[StageTrace]) -> list[dict[str, Any]]:
    """Reshapes pipeline StageTraces into the stage/strategy-model/tokens/
    cost/latency/status/error row shape shown in the Streamlit Trace tab, so
    any other consumer (a report script, a CLI) can render the same table."""
    return [
        {
            "stage": trace.stage,
            "strategy/model": f"{trace.strategy} / {trace.model or '-'}",
            "input_tokens": trace.input_tokens,
            "output_tokens": trace.output_tokens,
            "cost_usd": trace.cost_usd,
            "latency_ms": trace.latency_ms,
            "status": trace.status,
            "error": trace.error,
        }
        for trace in traces
    ]
