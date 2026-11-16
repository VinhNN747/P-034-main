import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AddModelScene, buildModelCreateInput, buildModelUpdate, canActivateProbedModel, capabilityPresentation, modelAdminReducer, modelDifficultyPresentation, modelFeatureLatencyMetrics, modelFeaturePricingMetrics, modelHealthLabel, modelHealthTone, ModelFeatureOverview, normalizeCapabilities, splitCapabilities, UnifiedModelsScene, type ModelAdminState } from "@/components/gateway/model-scenes";
import { toggleSortCriterion } from "@/components/gateway/multi-sort";
import { GatewayScene } from "@/components/gateway/gateway-scenes";
import type { ModelSummary } from "@/lib/gateway-types";
import { LanguageProvider } from "@/components/language-provider";

const anthropicModel: ModelSummary = {
  id: "haiku-id", provider_id: "anthropic-id", provider: "Anthropic", name: "claude-haiku-5-5",
  display_name: "Claude Haiku 5.5", status: "inactive", capabilities: ["text", "chat", "tool_use"],
  context_window: 1_000_000, target_latency_ms: null, last_probe_latency_ms: 3_000, max_output_tokens: 128_000,
  pricing: { input_per_million: 0.1, output_per_million: 0.5, version: "anthropic-docs-2026-10-17" },
  health_status: "unknown",
  feature_metadata: {
    schema_version: 1,
    upstream_features: {
      vision: { support: "supported", conditions: [], source_url: "https://docs.example.com/model" },
      forced_tool_choice: { support: "conditional", conditions: ["Depends on thinking mode."], source_url: "https://docs.example.com/model" },
      audio: { support: "unverified", conditions: [], source_url: "https://docs.example.com/model" },
    },
    gateway_features: {
      vision: { support: "unsupported", conditions: ["Images are not forwarded yet."], source_url: "https://docs.example.com/model" },
      tool_use: { support: "supported", conditions: [], source_url: "https://docs.example.com/model" },
    },
    constraints: { thinking_mode: "adaptive", default_effort: "medium", forced_tool_choice: "conditional", custom_sampling: false, prompt_caching: true, token_counting: true },
    pricing: { currency: "USD", standard_input_per_million: 0.1, standard_output_per_million: 0.5, long_context_input_threshold: 100_000, long_context_threshold_operator: ">", long_context_input_per_million: 0.5, long_context_output_per_million: 2.5, cache_write_5m_input_per_million: 0.125, cache_write_1h_input_per_million: 0.2, cache_read_input_per_million: 0.01, batch_discount_fraction: 0.5 },
    lifecycle: { stage: "legacy", reliable_knowledge_cutoff: "2026-06", retirement_commitment: "2027-10-07" },
    provenance: { checked_on: "2026-10-17", verification: "documentation_only", sources: ["https://docs.example.com/model"] },
    latency_reference: { source: "openrouter", provider: "Anthropic", service_tier: "standard", source_metric: "latency_p50", metric_interpretation: "ttft", latency_ms: 1_820, throughput_tokens_per_second: 177, percentile: 50, reasoning_effort: "all", locations: "all", window: "1 week", retrieved_on: "2026-10-17", source_url: "https://openrouter.ai/anthropic/claude-haiku-5.5/", verification: "external_reference" },
  },
};

describe("model scene routing", () => {
  it("routes catalog pages to live API-backed scenes", () => {
    const permissions = ["model_pools.read", "models.read", "models.manage"];
    expect(GatewayScene({ route: "/available-models", permissions })).toMatchObject({ type: UnifiedModelsScene });
    expect(GatewayScene({ route: "/models", permissions })).toMatchObject({ type: UnifiedModelsScene });
    expect(GatewayScene({ route: "/models/new", permissions })).toMatchObject({ type: AddModelScene });
  });
});

describe("model multi-sort", () => {
  it("keeps the first sort criterion primary when another column is added", () => {
    expect(toggleSortCriterion([{ field: "provider", dir: "asc" }], "input_price")).toEqual([
      { field: "provider", dir: "asc" },
      { field: "input_price", dir: "asc" },
    ]);
  });

  it("cycles a selected criterion without changing its priority", () => {
    expect(toggleSortCriterion([
      { field: "provider", dir: "asc" },
      { field: "input_price", dir: "asc" },
    ], "input_price")).toEqual([
      { field: "provider", dir: "asc" },
      { field: "input_price", dir: "desc" },
    ]);
  });

  it("removes a descending criterion on the third click", () => {
    expect(toggleSortCriterion([{ field: "context", dir: "desc" }], "context")).toEqual([]);
  });
});

describe("model health presentation", () => {
  it("maps persisted health states to distinct visual tones", () => {
    expect(modelHealthTone("healthy")).toBe("healthy");
    expect(modelHealthTone("degraded")).toBe("watch");
    expect(modelHealthTone("unhealthy")).toBe("incident");
    expect(modelHealthTone("unknown")).toBe("neutral");
  });

  it("turns machine health values into readable labels", () => {
    expect(modelHealthLabel("rate_limited")).toBe("Rate Limited");
    expect(modelHealthLabel(null)).toBe("Unknown");
  });
});

describe("model metadata presentation", () => {
  it("proxies machine capability keys into friendly labels and explanations", () => {
    expect(capabilityPresentation("structured_output")).toEqual({
      label: "Structured output",
      description: "Returns predictable, schema-friendly responses such as JSON.",
      icon: "{}",
    });
    expect(capabilityPresentation("tool-calling").label).toBe("Tool use");
    expect(capabilityPresentation("custom_capability").label).toBe("Custom Capability");
  });

  it("maps quality tiers to compact difficulty signals", () => {
    expect(modelDifficultyPresentation("simple")).toEqual({ label: "Simple", tone: "simple" });
    expect(modelDifficultyPresentation("medium")).toEqual({ label: "Mid", tone: "mid" });
    expect(modelDifficultyPresentation("hard")).toEqual({ label: "Hard", tone: "hard" });
    expect(modelDifficultyPresentation(undefined)).toEqual({ label: "Unrated", tone: "unknown" });
  });

  it("keeps the catalog baseline separate from probe and OpenRouter reference latency", () => {
    expect(modelFeatureLatencyMetrics(anthropicModel)).toEqual({
      targetLatencyMs: null,
      probeLatencyMs: 3_000,
      referenceLatencyMs: 1_820,
      referenceDate: "2026-10-17",
      referenceUrl: "https://openrouter.ai/anthropic/claude-haiku-5.5/",
    });
  });

  it("shows the effective long-context cache rates when input crosses the pricing tier", () => {
    expect(modelFeaturePricingMetrics(anthropicModel.feature_metadata!.pricing)).toEqual({
      standardCache: { write5m: 0.125, write1h: 0.2, read: 0.01 },
      longContextCache: { write5m: 0.625, write1h: 1, read: 0.05 },
    });
  });

  it("shows inactive catalog metadata, upstream and gateway support, pricing tiers, and external reference provenance", () => {
    const html = renderToStaticMarkup(createElement(LanguageProvider, null,
      createElement(ModelFeatureOverview, { model: anthropicModel }),
    ));
    expect(html).toContain("Không hoạt động");
    expect(html).toContain("Nhà cung cấp công bố");
    expect(html).toContain("Gateway hỗ trợ");
    expect(html.toLowerCase()).toContain("chưa kiểm tra trực tiếp");
    expect(html).toContain("Chưa xác minh");
    expect(html).toContain("100.000");
    expect(html).toContain("0,5");
    expect(html).toContain("Giá cache khi input &gt; 100.000 token");
    expect(html).toContain("0,05 USD / 1M");
    expect(html).toContain("128.000");
    expect(html).toContain("1.820 ms");
    expect(html).toContain("P50");
    expect(html).toContain("TTFT");
    expect(html).toContain("2026-10-17");
    expect(html).toContain("openrouter.ai/anthropic/claude-haiku-5.5/");
    expect(html).toContain("3.000 ms");
    expect(html).toMatch(/Latency baseline nội bộ<\/dt><dd>—<\/dd>/);
    expect(html).not.toMatch(/<dd>0 ms<\/dd>/);
  });
});

describe("model metadata form", () => {
  it("normalizes capabilities without silently assigning chat", () => {
    expect(normalizeCapabilities(["embedding", " embedding ", "rerank"])).toEqual(["embedding", "rerank"]);
    expect(normalizeCapabilities([])).toEqual([]);
  });

  it("keeps custom capability labels editable without turning them into built-in options", () => {
    expect(splitCapabilities(["chat", "vendor_feature", "embedding"])).toEqual({ selected: ["chat", "embedding"], custom: ["vendor_feature"] });
  });

  it("only enables activation for a healthy probe on the current credential version", () => {
    const currentKey = { probeStatus: "healthy", probeCredentialVersion: 4, providerKind: "openai", credentialConfigured: true, credentialStatus: "active", currentCredentialVersion: 4 };
    expect(canActivateProbedModel(currentKey)).toBe(true);
    expect(canActivateProbedModel({ ...currentKey, currentCredentialVersion: 5 })).toBe(false);
    expect(canActivateProbedModel({ ...currentKey, probeStatus: "unavailable" })).toBe(false);
    expect(canActivateProbedModel({ ...currentKey, probeStatus: "offline" })).toBe(false);
    expect(canActivateProbedModel({ probeStatus: "offline", providerKind: "local" })).toBe(true);
  });

  it("does not reset a model probe for price-only metadata edits", () => {
    const current: ModelSummary = { id: "m1", provider_id: "p1", provider: "OpenAI", name: "gpt", display_name: "GPT", status: "active", capabilities: ["chat"], context_window: 128000, target_latency_ms: 900, quality_tier: "mid", pricing: { input_per_million: 1, output_per_million: 2, version: "v1" }, health_status: "healthy", last_probe_credential_version: 4 };
    expect(buildModelUpdate(current, { displayName: "GPT", capabilities: ["chat"], qualityTier: "mid", contextWindow: "128000", targetLatency: "900", inputPrice: "1.5", outputPrice: "2", pricingVersion: "v1" })).toEqual({ input_price_per_million: 1.5 });
  });

  it("creates complete inactive model metadata without adding chat to embedding-only models", () => {
    expect(buildModelCreateInput("provider-1", "embed-v2", { displayName: "Embedding v2", capabilities: ["embedding"], qualityTier: "mid", contextWindow: "8192", targetLatency: "500", inputPrice: "0.1", outputPrice: "0", pricingVersion: "2026-10" })).toEqual({
      provider_id: "provider-1", name: "embed-v2", display_name: "Embedding v2", status: "inactive", capabilities: ["embedding"], quality_tier: "mid", context_window: 8192, target_latency_ms: 500, input_price_per_million: 0.1, output_price_per_million: 0, pricing_version: "2026-10",
    });
  });
});

describe("admin model editor state", () => {
  it("refreshes the catalog after a probe while keeping the editor open for activation", () => {
    const model = { id: "model-1", provider: "OpenAI", name: "gpt-4.1", display_name: "GPT-4.1", status: "inactive", capabilities: ["chat"], pricing: { input_per_million: 1, output_per_million: 2 } } as ModelSummary;
    const initial: ModelAdminState = { selectedModel: model, catalogRevision: 0 };
    expect(modelAdminReducer(initial, { type: "refresh" })).toEqual({ selectedModel: model, catalogRevision: 1 });
    expect(modelAdminReducer(initial, { type: "close" })).toEqual({ selectedModel: null, catalogRevision: 0 });
  });
});
