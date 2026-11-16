import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { RequestRoutingMap } from "@/components/gateway/request-scenes";
import type { GatewayRequestDetail } from "@/lib/gateway-types";

const detail: GatewayRequestDetail = {
  request_id: "req-map-1",
  application_id: "app-support",
  source: "playground",
  status: "partial",
  aggregation_status: "partial",
  fallback_used: true,
  fallback_reason: "Primary model timed out",
  output: "A synthesized answer",
  tasks: [],
  traces: [],
  executions: [],
  aggregation: null,
};

describe("request routing map", () => {
  it("renders the persisted route and execution states", () => {
    const markup = renderToStaticMarkup(<RequestRoutingMap
      detail={detail}
      tasks={[{ task_id: "goal-1", description: "Find the policy" }]}
      executions={[
        { task_id: "goal-1", model_name: "gpt-5-mini", provider_name: "OpenAI", status: "completed" },
        { task_id: "goal-1", model_name: "claude-sonnet", provider_name: "Anthropic", status: "timeout", fallback: true },
      ]}
      aggregation={{ strategy: "best_effort", status: "partial", final_answer: "A synthesized answer" }}
    />);

    expect(markup).toContain("Sơ đồ định tuyến yêu cầu");
    expect(markup).toContain("gpt-5-mini");
    expect(markup).toContain("claude-sonnet");
    expect(markup).toContain("request-routing-node--fallback");
    expect(markup).toContain("Phản hồi cuối đã sẵn sàng");
  });
});
