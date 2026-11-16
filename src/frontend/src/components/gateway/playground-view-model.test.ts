import { describe, expect, it } from "vitest";

import { toPlaygroundViewModel } from "@/components/gateway/playground-view-model";

describe("toPlaygroundViewModel", () => {
  it("maps persisted runtime output and trace rows without simulated values", () => {
    const view = toPlaygroundViewModel({
      request_id: "req-1",
      application_id: "app-1",
      source: "playground",
      status: "completed",
      output: "offline answer",
      provider: "local",
      model: "local-deterministic",
      latency_ms: 3,
      input_tokens: 4,
      output_tokens: 5,
      total_tokens: 9,
      estimated_cost: 0,
      fallback_used: false,
      fallback_reason: null,
      trace_count: 2,
      tasks: [],
      traces: [
        { stage: "understand", strategy: "rule", status: "success", latency_ms: 0 },
        { stage: "route_execute:task-1", strategy: "database", status: "success", latency_ms: 3 },
      ],
      executions: [],
      aggregation: { status: "complete" },
    });

    expect(view).toMatchObject({
      requestId: "req-1",
      output: "offline answer",
      provider: "Offline",
      model: "local-deterministic",
      latency: "3ms",
      tokens: "9 tokens",
    });
    expect(view.traces).toHaveLength(2);
  });

  it("labels each persisted subtask in a multi-task response", () => {
    const view = toPlaygroundViewModel({
      request_id: "req-2",
      source: "playground",
      status: "completed",
      output: "combined answer",
      tasks: [
        { task_id: "task-1", description: "Summarize the report" },
        { task_id: "task-2", description: "Draft the email" },
      ],
      executions: [
        { task_id: "task-1", status: "success", result_payload: "Summary" },
        { task_id: "task-2", status: "success", result_payload: "Email" },
      ],
      traces: [],
    });

    expect(view.output).toContain("## Task 1\n\nSummary");
    expect(view.output).toContain("## Task 2\n\nEmail");
    expect(view.tasks.map((task) => task.label)).toEqual(["Task 1", "Task 2"]);
  });

  it("labels a successful fallback without presenting it as a failed task", () => {
    const view = toPlaygroundViewModel({
      request_id: "req-3",
      source: "playground",
      status: "completed",
      output: "combined answer",
      fallback_used: true,
      fallback_reason: "Gemini API returned HTTP 503",
      tasks: [{ task_id: "task-1", description: "Analyze" }],
      executions: [{
        task_id: "task-1",
        status: "success",
        fallback: true,
        result_payload: "Answer",
        failure_reason: "Gemini API returned HTTP 503",
      }],
      traces: [],
    });

    expect(view.tasks[0]).toMatchObject({
      status: "success · fallback",
      fallback: true,
    });
    expect(view.tasks[0].error).toBe("Gemini API returned HTTP 503");
    expect(view.fallback).toBe("Fallback used for Task 1: Gemini API returned HTTP 503");
  });
});
