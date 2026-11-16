from src.shared.dtos.gateway import CostSummary, StageTrace


def summarize_costs(traces: list[StageTrace]) -> CostSummary:
    attachment = sum(trace.cost_usd for trace in traces if trace.stage == "attachment_analysis")
    understand = sum(
        trace.cost_usd
        for trace in traces
        if trace.stage == "understand"
        or (trace.stage == "constraint_resolution" and trace.metadata.get("semantic_model_call"))
    )
    # decompose, plus the split gate and subtask scoring that decide whether a split is kept.
    decomposition = sum(trace.cost_usd for trace in traces if trace.stage.startswith("decompose"))
    validation = sum(
        trace.cost_usd for trace in traces if trace.stage in {"request_validation", "decomposition_validation"}
    )
    routing = sum(trace.cost_usd for trace in traces if trace.stage.startswith("route_execute:"))
    aggregation = sum(trace.cost_usd for trace in traces if trace.stage == "aggregate")
    pre_routing = attachment + understand + decomposition + validation
    return CostSummary(
        attachment_cost=attachment,
        understand_cost=understand,
        decomposition_cost=decomposition,
        validation_cost=validation,
        total_pre_routing_cost=pre_routing,
        routing_cost=routing,
        aggregation_cost=aggregation,
        total_cost=pre_routing + routing + aggregation,
        estimated=any(trace.metadata.get("cost_estimated", False) for trace in traces),
    )
