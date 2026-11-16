import { describe, expect, it } from "vitest";

import {
  coveredTiers,
  draftProblem,
  offlineErrorMessage,
  offlineSnippet,
  pipelineEndpoints,
  presetFor,
  slugify,
  tierOptions,
  type OfflineDraft,
} from "@/components/gateway/offline-pipeline-scenes";
import { GatewayApiError } from "@/lib/gateway-api";
import { getNavigation, getRouteViewState } from "@/lib/gateway-navigation";
import type { CustomerEndpoint, EndpointModel } from "@/lib/gateway-types";

const t = ((key: string) => key) as Parameters<typeof offlineErrorMessage>[1];

function model(overrides: Partial<EndpointModel>): EndpointModel {
  return {
    id: "m", name: "qwen", display_name: "qwen", quality_tier: "simple", context_window: null, capabilities: ["chat", "text"],
    status: "active", health_status: "healthy", last_probe_at: null, last_probe_latency_ms: null, last_probe_error: null, ...overrides,
  };
}

function endpoint(id: string, models: EndpointModel[]): CustomerEndpoint {
  return {
    id, display_name: `GPU ${id}`, base_url: "https://gpu.example.com/v1", status: "active", has_api_key: false,
    key_last4: null, models, created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z",
  };
}

const draft = (overrides: Partial<OfflineDraft> = {}): OfflineDraft => ({
  name: "Chatbot nội bộ", slug: "chatbot-noi-bo", description: "", models: { simple: "a", mid: "b", hard: "c" },
  mid: 0.23, large: 0.61, maxOutputTokens: 800, timeoutSeconds: 60, fallbackEnabled: true, ...overrides,
});

describe("offline pipelines", () => {
  it("lists the endpoints a pipeline runs on with the tiers each serves", () => {
    const ladder = (tier: "simple" | "mid" | "hard", endpointId: string) => ({
      tier, model_id: tier, name: tier, display_name: tier, endpoint_id: endpointId, endpoint_name: `GPU ${endpointId}`,
      status: "active", health_status: "healthy", context_window: null,
    });
    const pipeline = { models: [ladder("simple", "a"), ladder("mid", "b"), ladder("hard", "a")] } as unknown as Parameters<typeof pipelineEndpoints>[0];
    expect(pipelineEndpoints(pipeline)).toEqual([
      { id: "a", name: "GPU a", tiers: ["simple", "hard"] },
      { id: "b", name: "GPU b", tiers: ["mid"] },
    ]);
  });

  it("names the preset a threshold pair belongs to", () => {
    expect(presetFor(0.23, 0.61)).toBe("balanced");
    expect(presetFor(0.25, 0.65)).toBe("saver");
    expect(presetFor(0.25, 0.55)).toBe("quality");
    expect(presetFor(0.2, 0.6)).toBe("custom");
  });

  it("derives a slug from a Vietnamese name", () => {
    expect(slugify("Chatbot nội bộ")).toBe("chatbot-noi-bo");
    expect(slugify("  Đội CSKH / Hà Nội!  ")).toBe("doi-cskh-ha-noi");
  });

  it("checks a draft before sending it", () => {
    expect(draftProblem(draft())).toBeNull();
    expect(draftProblem(draft({ name: " " }))).toBe("offline.errorName");
    expect(draftProblem(draft({ slug: "Bad Slug" }))).toBe("offline.errorSlug");
    expect(draftProblem(draft({ models: { simple: "a", mid: "b" } }))).toBe("offline.errorModels");
    expect(draftProblem(draft({ models: { simple: "a", mid: "a", hard: "c" } }))).toBe("offline.errorModels");
    expect(draftProblem(draft({ mid: 0.7, large: 0.6 }))).toBe("offline.errorThresholds");
    expect(draftProblem(draft({ mid: Number.NaN }))).toBe("offline.errorThresholds");
  });

  it("offers only probed models of the tier, from every endpoint", () => {
    const endpoints = [
      endpoint("1", [model({ id: "s1", quality_tier: "simple" }), model({ id: "m1", quality_tier: "mid", status: "inactive" })]),
      endpoint("2", [model({ id: "s2", name: "llama", quality_tier: "simple" }), model({ id: "h2", quality_tier: "hard" })]),
    ];
    expect(tierOptions(endpoints, "simple").map((item) => item.id)).toEqual(["s1", "s2"]);
    expect(tierOptions(endpoints, "simple")[1].label).toBe("llama · GPU 2");
    expect(tierOptions(endpoints, "mid")).toEqual([]);
    expect(coveredTiers(endpoints)).toBe(2);
  });

  it("shows why the backend refused, but keeps server errors generic", () => {
    expect(offlineErrorMessage(new GatewayApiError(422, "unprocessable", "Mid: probe qwen first"), t)).toBe("Mid: probe qwen first");
    expect(offlineErrorMessage(new GatewayApiError(409, "conflict", "Slug exists"), t)).toBe("Slug exists");
    expect(offlineErrorMessage(new GatewayApiError(500, "internal", "Traceback"), t)).toBe("offline.errorGeneric");
    expect(offlineErrorMessage(new GatewayApiError(403, "forbidden", "x"), t)).toBe("offline.errorPermission");
  });

  it("gives an OpenAI snippet with the off_ key and the auto model", () => {
    const snippet = offlineSnippet("https://ai.example/v1");
    expect(snippet).toContain('base_url="https://ai.example/v1"');
    expect(snippet).toContain('api_key="off_..."');
    expect(snippet).toContain('model="auto"');
    expect(snippet).toContain('"Idempotency-Key"');  // the gateway refuses generation requests without it
  });

  it("is a section of its own, and building one needs the pool right too", () => {
    const reader = ["applications.read_own"];
    const builder = ["applications.read_own", "applications.manage_own", "model_pools.manage_own"];
    expect(getNavigation(reader).map((item) => item.href)).toContain("/offline");
    expect(getRouteViewState("/offline", reader)).toBe("allowed");
    expect(getRouteViewState("/offline/abc", reader)).toBe("allowed");
    expect(getRouteViewState("/offline/new", reader)).toBe("restricted");
    expect(getRouteViewState("/offline/new", ["applications.read_own", "applications.manage_own"])).toBe("restricted");
    expect(getRouteViewState("/offline/new", builder)).toBe("allowed");
    expect(getRouteViewState("/offline/abc/settings", builder)).toBe("allowed");
    expect(getRouteViewState("/offline/abc/playground", ["applications.read_own"])).toBe("allowed");
  });
});
