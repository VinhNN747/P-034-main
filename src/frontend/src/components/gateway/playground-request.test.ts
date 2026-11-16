import { describe, expect, it } from "vitest";

import { buildPlaygroundGatewayRequest } from "@/components/gateway/playground-request";

describe("buildPlaygroundGatewayRequest", () => {
  it("requests automatic aggregation for multi-goal Playground prompts", () => {
    const request = buildPlaygroundGatewayRequest(
      "app-1",
      "Calculate 27 x 14 and translate the result into English.",
    );

    expect(request).toMatchObject({
      application_id: "app-1",
      prompt: "Calculate 27 x 14 and translate the result into English.",
      source: "playground",
      router: "database",
      decomposer: "llm",
      aggregator: "deterministic",
    });
  });
});
