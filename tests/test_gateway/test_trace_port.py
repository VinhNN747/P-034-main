from src.backend_ai.gateway.observability.trace import TraceCollector, trace_timer


def test_ported_trace_collector_records_stage_trace():
    collector = TraceCollector("trace-port-1")
    started, timer = trace_timer()

    trace = collector.add(
        stage="understand",
        strategy="rule",
        started_at=started,
        timer_started=timer,
        metadata={"test": True},
    )

    assert trace.request_id == "trace-port-1"
    assert trace.stage == "understand"
    assert trace.metadata == {"test": True}
