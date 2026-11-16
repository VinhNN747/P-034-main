import { describe, expect, it } from "vitest";

import { toApplicationViewModel } from "@/components/gateway/application-view-model";

describe("toApplicationViewModel", () => {
  it("maps persisted application and gateway configuration fields", () => {
    const result = toApplicationViewModel({
      id: "app-1",
      slug: "support",
      name: "Support",
      description: null,
      owner_user_id: "user-1",
      environment: "production",
      status: "active",
      tags: [],
      request_count: 12,
      gateway_config: {
        application_id: "app-1",
        version: 4,
        model_pool_id: "pool-1",
        routing_policy_id: "policy-1",
        routing_objective: "Lowest cost",
        model_ids: ["gpt-4.1", "claude-sonnet"],
        fallback_model_ids: ["gpt-4.1-mini"],
        budget_ceiling: 250,
        warning_threshold: 0.8,
        budget_action: "block",
        max_output_tokens: 800,
        timeout_seconds: 30,
        retry_limit: 2,
        fallback_enabled: true,
      },
    });

    expect(result).toMatchObject({
      id: "app-1",
      name: "Support",
      objective: "Lowest cost",
      models: ["gpt-4.1", "claude-sonnet"],
      requestCount: 12,
      configVersion: 4,
      budget: "250.00",
    });
  });
});
