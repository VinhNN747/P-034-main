import { describe, expect, it } from "vitest";

import { activeTierCount, endpointErrorMessage } from "@/components/gateway/endpoint-scenes";
import { GatewayApiError } from "@/lib/gateway-api";
import { getNavigation, getRouteViewState } from "@/lib/gateway-navigation";
import type { CustomerEndpoint, EndpointModel } from "@/lib/gateway-types";

const t = ((key: string) => key) as Parameters<typeof endpointErrorMessage>[1];

function model(overrides: Partial<EndpointModel>): EndpointModel {
  return {
    id: "m", name: "qwen", display_name: "qwen", quality_tier: "simple", context_window: null, capabilities: ["chat", "text"], status: "inactive",
    health_status: "unknown", last_probe_at: null, last_probe_latency_ms: null, last_probe_error: null, ...overrides,
  };
}

function endpoint(models: EndpointModel[]): CustomerEndpoint {
  return {
    id: "e", display_name: "GPU", base_url: "https://gpu.example.com/v1", status: "active", has_api_key: false,
    key_last4: null, models, created_at: "2026-10-14T00:00:00Z", updated_at: "2026-10-14T00:00:00Z",
  };
}

describe("my endpoints", () => {
  it("shows why the backend refused a URL, but keeps server errors generic", () => {
    const refused = new GatewayApiError(422, "unprocessable", "The endpoint must be reachable on the public internet.");
    expect(endpointErrorMessage(refused, t)).toBe("The endpoint must be reachable on the public internet.");
    expect(endpointErrorMessage(new GatewayApiError(500, "internal", "Traceback ..."), t)).toBe("endpoints.errorGeneric");
    expect(endpointErrorMessage(new GatewayApiError(403, "forbidden", "Insufficient role"), t)).toBe("endpoints.errorPermission");
    expect(endpointErrorMessage(new GatewayApiError(503, "unavailable", "down"), t)).toBe("endpoints.errorUnavailable");
  });

  it("counts the tiers that have an active model across endpoints", () => {
    expect(activeTierCount([endpoint([model({ quality_tier: "simple" })])])).toBe(0);
    expect(activeTierCount([
      endpoint([model({ id: "a", quality_tier: "simple", status: "active" }), model({ id: "b", quality_tier: "simple", status: "active" })]),
      endpoint([model({ id: "c", quality_tier: "hard", status: "active" }), model({ id: "d", quality_tier: "mid" })]),
    ])).toBe(2);
  });

  it("is reachable for users who manage their own pools", () => {
    // One "Offline" item; without the pipelines right it opens My endpoints directly.
    expect(getNavigation(["model_pools.read_own"]).map((item) => item.href)).toContain("/my-endpoints");
    const both = getNavigation(["applications.read_own", "model_pools.read_own"]).map((item) => item.href);
    expect(both).toContain("/offline");
    expect(both).not.toContain("/my-endpoints");
    expect(getRouteViewState("/my-endpoints", ["model_pools.read_own"])).toBe("allowed");
    expect(getRouteViewState("/my-endpoints", ["models.read_available"])).toBe("restricted");
  });
});
