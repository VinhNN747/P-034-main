import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const authState = vi.hoisted(() => ({
  getSession: vi.fn(),
  refreshSession: vi.fn(),
}));

vi.mock("@/lib/supabase/client", () => ({
  createClient: () => ({ auth: authState }),
}));

import {
  GatewayApiError,
  apiFetch,
  createGatewayRequest,
  getGatewayRequest,
  getApplication,
  listAvailableModels,
  createModel,
  createProviderConnection,
  revokeProviderCredential,
  replaceProviderCredential,
  probeProviderConnection,
  setServiceProviderBinding,
  updateModel,
  reportIncident,
  listIncidentReports,
  updateIncident,
} from "@/lib/gateway-api";
import {
  createEmptyState,
  createErrorState,
  createLoadingState,
  createSuccessState,
  gatewayQueryKeys,
} from "@/lib/gateway-query";
import { invalidationKeysForMutation } from "@/lib/gateway-mutations";
import type { ModelCreateInput } from "@/lib/gateway-types";

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("gateway API foundation", () => {
  beforeEach(() => {
    authState.getSession.mockReset();
    authState.refreshSession.mockReset();
    authState.getSession.mockResolvedValue({
      data: { session: { access_token: "access-token-1" } },
      error: null,
    });
    authState.refreshSession.mockResolvedValue({
      data: { session: { access_token: "access-token-2" } },
      error: null,
    });
    vi.clearAllMocks();
  });

  it("injects the current Supabase bearer token into API requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        id: "app-1",
        name: "Demo",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await getApplication("app-1");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/applications/app-1",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer access-token-1",
          Accept: "application/json",
        }),
      }),
    );
  });

  it("passes catalog output limits and feature metadata through to the API", async () => {
    const payload: ModelCreateInput = {
      provider_id: "anthropic-1",
      name: "claude-haiku-5-5",
      display_name: "Claude Haiku 5.5",
      max_output_tokens: 128_000,
      feature_metadata: {
        schema_version: 1,
        upstream_features: {},
        gateway_features: {},
        constraints: { thinking_mode: "adaptive", default_effort: "medium", forced_tool_choice: "unsupported", custom_sampling: false, prompt_caching: true, token_counting: true },
        pricing: { currency: "USD", standard_input_per_million: 0.1, standard_output_per_million: 0.5, cache_write_5m_input_per_million: 0.125, cache_write_1h_input_per_million: 0.2, cache_read_input_per_million: 0.01 },
        lifecycle: { stage: "active" },
        provenance: { checked_on: "2026-10-17", verification: "documentation_only", sources: ["https://docs.example.com/model"] },
        latency_reference: null,
      },
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: "model-1", ...payload }));
    vi.stubGlobal("fetch", fetchMock);

    await createModel(payload);

    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/api/v1/gateway/models", expect.objectContaining({
      method: "POST",
      body: JSON.stringify(payload),
    }));
  });

  it("submits user reports to the management backend and reads only the reporter endpoint", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => jsonResponse({ id: "ticket-1" }, 201));
    vi.stubGlobal("fetch", fetchMock);
    const input = { title: "Fallback failed", description: "No response", type: "fallback" as const, request_id: "req-1" };
    await reportIncident(input);
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/api/v1/gateway/incident-reports", expect.objectContaining({
      method: "POST", body: JSON.stringify(input), headers: expect.objectContaining({ Authorization: "Bearer access-token-1" }),
    }));
    await listIncidentReports({ page: 2, page_size: 10, status: "resolved" });
    expect(fetchMock).toHaveBeenLastCalledWith("http://localhost:8000/api/v1/gateway/incident-reports?page=2&page_size=10&status=resolved", expect.anything());
    await updateIncident("ticket/1", { status: "resolved", owner: "Support", resolution_note: "Fixed" });
    expect(fetchMock).toHaveBeenLastCalledWith("http://localhost:8000/api/v1/gateway/incidents/ticket%2F1", expect.objectContaining({
      method: "PATCH", body: JSON.stringify({ status: "resolved", owner: "Support", resolution_note: "Fixed" }),
    }));
  });

  it("refreshes the Supabase session once and retries a 401 request", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, 401))
      .mockResolvedValueOnce(jsonResponse({ id: "app-1" }));
    vi.stubGlobal("fetch", fetchMock);

    await getApplication("app-1");

    expect(authState.refreshSession).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][1]).toEqual(
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer access-token-2",
        }),
      }),
    );
  });

  it("normalizes 403 errors without exposing backend internals", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        { detail: "sqlalchemy secret and internal stack trace" },
        403,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = listAvailableModels();

    await expect(result).rejects.toMatchObject({
      status: 403,
      code: "forbidden",
      message: "You do not have permission to perform this action.",
    });
    await expect(result).rejects.not.toThrow("sqlalchemy");
  });

  it.each([
    { detail: "Application is inactive" },
    { code: "APPLICATION_INACTIVE", message: "The application is inactive." },
  ])("distinguishes inactive applications from missing permissions: %j", async (payload) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(payload, 403)));

    await expect(createGatewayRequest({ application_id: "app-1", prompt: "hello" }))
      .rejects.toMatchObject({
        status: 403,
        code: "APPLICATION_INACTIVE",
        message: "This application is inactive. Activate it before sending requests.",
      });
  });

  it("surfaces FastAPI detail for validation errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ detail: "No routing candidate satisfies capabilities and constraints" }, 422),
      ),
    );

    const result = listAvailableModels();
    await expect(result).rejects.toMatchObject({
      status: 422,
      code: "validation_error",
      message: "No routing candidate satisfies capabilities and constraints",
    });
  });

  it("surfaces service errors instead of falling back to local fixture data", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "provider failed" }, 503)),
    );

    const result = listAvailableModels();
    await expect(result).rejects.toBeInstanceOf(GatewayApiError);
    await expect(result).rejects.toMatchObject({
      status: 503,
      code: "service_unavailable",
    });
  });
});

describe("separate management and AI backends", () => {
  afterEach(() => vi.unstubAllEnvs());
  beforeEach(() => {
    authState.getSession.mockResolvedValue({
      data: { session: { access_token: "access-token-1" } }, error: null,
    });
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://management.example");
    vi.stubEnv("NEXT_PUBLIC_AI_API_URL", "https://ai.example");
  });

  it("sends execution to AI and request history to management", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => jsonResponse({ request_id: "req-1" }));
    vi.stubGlobal("fetch", fetchMock);
    await createGatewayRequest({ application_id: "app-1", prompt: "hello" });
    await getGatewayRequest("req-1");
    await apiFetch("/api/v1/gateway/requests?page=2");
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "https://ai.example/api/v1/gateway/requests",
      "https://management.example/api/v1/gateway/requests/req-1",
      "https://management.example/api/v1/gateway/requests?page=2",
    ]);
  });

  it("keeps token refresh on the AI URL", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, 401))
      .mockResolvedValueOnce(jsonResponse({ request_id: "req-1" }));
    authState.refreshSession.mockResolvedValue({ data: { session: { access_token: "new-token" } }, error: null });
    vi.stubGlobal("fetch", fetchMock);
    await createGatewayRequest({ application_id: "app-1", prompt: "hello" });
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "https://ai.example/api/v1/gateway/requests",
      "https://ai.example/api/v1/gateway/requests",
    ]);
    expect(fetchMock.mock.calls[1][1].headers.Authorization).toBe("Bearer new-token");
  });

  it("routes a trailing-slash execution request to AI", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => jsonResponse({ request_id: "req-1" }));
    vi.stubGlobal("fetch", fetchMock);
    await apiFetch("/api/v1/gateway/requests/?source=playground", { method: "POST" });
    expect(fetchMock.mock.calls[0][0]).toBe("https://ai.example/api/v1/gateway/requests/?source=playground");
  });
});

describe("gateway query state and keys", () => {
  it("represents loading, success, empty and error states explicitly", () => {
    expect(createLoadingState()).toEqual({ status: "loading" });
    expect(createSuccessState({ id: "app-1" })).toEqual({
      status: "success",
      data: { id: "app-1" },
    });
    expect(createEmptyState([])).toEqual({ status: "empty", data: [] });
    expect(createErrorState(new GatewayApiError(403, "forbidden", "Denied"))).toEqual(
      expect.objectContaining({
        status: "error",
        error: expect.objectContaining({ code: "forbidden" }),
      }),
    );
  });

  it("isolates query keys by resource, application and every filter", () => {
    expect(gatewayQueryKeys.application("app-1")).not.toEqual(
      gatewayQueryKeys.application("app-2"),
    );
    expect(
      gatewayQueryKeys.applications({ environment: "production", page: 1 }),
    ).not.toEqual(gatewayQueryKeys.applications({ environment: "staging", page: 1 }));
    expect(
      gatewayQueryKeys.usage({ application_id: "app-1", model_id: "model-a" }),
    ).not.toEqual(gatewayQueryKeys.usage({ application_id: "app-1", model_id: "model-b" }));
    expect(
      gatewayQueryKeys.usage({ model_id: "model-a", application_id: "app-1" }),
    ).toEqual(gatewayQueryKeys.usage({ application_id: "app-1", model_id: "model-a" }));
  });
});

describe("provider connection credential requests", () => {
  beforeEach(() => {
    authState.getSession.mockResolvedValue({ data: { session: { access_token: "access-token-1" } }, error: null });
  });

  it("sends a new key once in the create request and only receives a credential summary", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: "provider-1", credential: { configured: true, version: 1 } }));
    vi.stubGlobal("fetch", fetchMock);
    const summary = await createProviderConnection({ name: "primary-openai", display_name: "Primary OpenAI", kind: "openai", purpose: "target", api_key: "sk-test-secret" });
    const request = fetchMock.mock.calls[0][1];
    expect(JSON.parse(request.body)).toMatchObject({ api_key: "sk-test-secret" });
    expect(summary).not.toHaveProperty("api_key");
    expect(JSON.stringify(summary)).not.toContain("sk-test-secret");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("sends replace version and probe mode without putting a key in the probe request", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ id: "provider-1", credential: { configured: true, version: 2 } }))
      .mockResolvedValueOnce(jsonResponse({ provider_id: "provider-1", credential_version: 2, status: "healthy", mode: "chat", model_name: "gpt-4.1" }))
      .mockResolvedValueOnce(jsonResponse({ provider_id: "jev-internal", credential_version: 7, status: "healthy", mode: "jev", model_name: null }));
    vi.stubGlobal("fetch", fetchMock);
    await replaceProviderCredential("provider-1", { api_key: "sk-replacement", expected_version: 1 });
    await probeProviderConnection("provider-1", { expected_version: 2, mode: "chat", model_name: "gpt-4.1" });
    await probeProviderConnection("jev-internal", { expected_version: 7, mode: "jev" });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ api_key: "sk-replacement", expected_version: 1 });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ expected_version: 2, mode: "chat", model_name: "gpt-4.1" });
    expect(fetchMock.mock.calls[1][0]).toContain("/provider-connections/provider-1/probe");
    expect(fetchMock.mock.calls[2][0]).toContain("/provider-connections/jev-internal/probe");
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({ expected_version: 7, mode: "jev" });
    expect(JSON.stringify(fetchMock.mock.calls[2][1].body)).not.toContain("api_key");
  });

  it("forwards expected versions for revocation, service binding and model activation", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse([{ purpose: "routing_jev", provider_id: "jev-internal" }]))
      .mockResolvedValueOnce(jsonResponse({ id: "model-1", status: "active" }));
    vi.stubGlobal("fetch", fetchMock);
    await revokeProviderCredential("provider-1", 3);
    await setServiceProviderBinding("routing_jev", { provider_id: "jev-internal" });
    await updateModel("model-1", { status: "active" });
    expect(fetchMock.mock.calls.map(([url, init]) => [url, init.method])).toEqual([
      ["http://localhost:8000/api/v1/gateway/provider-connections/provider-1/credential?expected_version=3", "DELETE"],
      ["http://localhost:8000/api/v1/gateway/service-provider-bindings/routing_jev", "PUT"],
      ["http://localhost:8000/api/v1/gateway/models/model-1", "PATCH"],
    ]);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ provider_id: "jev-internal" });
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({ status: "active" });
  });
});

describe("gateway mutation invalidation", () => {
  it("returns targeted keys for application config and runtime mutations", () => {
    expect(
      invalidationKeysForMutation({
        kind: "gateway-config.updated",
        applicationId: "app-1",
      }),
    ).toEqual([
      gatewayQueryKeys.application("app-1"),
      gatewayQueryKeys.gatewayConfig("app-1"),
    ]);

    expect(
      invalidationKeysForMutation({
        kind: "gateway-request.created",
        applicationId: "app-1",
        requestId: "req-1",
      }),
    ).toEqual([
      gatewayQueryKeys.request("req-1"),
      gatewayQueryKeys.requests({ applicationId: "app-1" }),
      gatewayQueryKeys.overview({ application_id: "app-1" }),
      gatewayQueryKeys.usage({ application_id: "app-1" }),
    ]);
  });

  it("covers model, incident, policy review and billing mutations", () => {
    expect(
      invalidationKeysForMutation({ kind: "model.created", modelId: "model-1" }),
    ).toContainEqual(gatewayQueryKeys.models({}));
    expect(
      invalidationKeysForMutation({ kind: "incident.updated", incidentId: "inc-1" }),
    ).toContainEqual(gatewayQueryKeys.incidents({}));
    expect(
      invalidationKeysForMutation({
        kind: "policy-review.decision",
        reviewId: "review-1",
      }),
    ).toContainEqual(gatewayQueryKeys.policyReviews({}));
    expect(
      invalidationKeysForMutation({ kind: "billing.topup" }),
    ).toEqual(
      expect.arrayContaining([
        gatewayQueryKeys.billingAccount(),
        gatewayQueryKeys.billingTransactions({}),
      ]),
    );
  });

  it("invalidates provider summaries and dependent catalog views without a credential payload", () => {
    expect(invalidationKeysForMutation({ kind: "provider.credential_updated", providerId: "provider-1" })).toEqual(expect.arrayContaining([
      gatewayQueryKeys.providerConnections(),
      gatewayQueryKeys.models(),
      gatewayQueryKeys.availableModels(),
      gatewayQueryKeys.modelPools(),
    ]));
  });
});
