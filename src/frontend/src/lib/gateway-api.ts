import { createClient } from "@/lib/supabase/client";

import type {
  ApiErrorPayload,
  ApplicationCreateInput,
  ApplicationDetail,
  ApplicationListParams,
  ApplicationListResponse,
  ApplicationModelsResponse,
  ApplicationKey,
  ApplicationKeyCreateInput,
  ApplicationKeyCreated,
  ApplicationKeyPage,
  ApplicationUpdateInput,
  BillingAccount,
  BillingSettings,
  BillingSettingsInput,
  BillingTopupInput,
  BillingTopupResponse,
  BillingTransactionPage,
  ComparisonAccepted,
  ComparisonReport,
  ComparisonCreateInput,
  ComparisonRun,
  Incident,
  IncidentCreateInput,
  IncidentParams,
  IncidentUpdateInput,
  IncidentReportInput,
  GatewayConfig,
  GatewayConfigMutationInput,
  GatewayRequestDetail,
  GatewayRequestInput,
  GatewayRequestListParams,
  GatewayRequestPage,
  GatewayRequestResponse,
  ModelCreateInput,
  ModelListParams,
  ModelListResponse,
  ModelProbeResponse,
  ModelUpdateInput,
  FallbackSuggestion,
  ModelPool,
  ModelPoolMutationInput,
  ModelSummary,
  OverviewParams,
  PageResponse,
  ProviderSummary,
  CustomerEndpoint,
  CustomerEndpointCreateInput,
  OfflinePipeline,
  OfflinePipelineCreateInput,
  OfflinePipelineUpdateInput,
  EndpointModelCreateInput,
  ProviderConnection,
  ProviderConnectionCreateInput,
  ProviderConnectionUpdateInput,
  ProviderCredentialReplaceInput,
  ProviderConnectionProbeInput,
  ProviderConnectionProbeResult,
  ServiceProviderBinding,
  ServiceProviderBindingUpdateInput,
  OverviewResponse,
  PolicyDecisionInput,
  PolicyReview,
  PolicyReviewParams,
  UsageParams,
  UsageResponse,
} from "@/lib/gateway-types";

const DEFAULT_API_URL = "http://localhost:8000";
const DEFAULT_AI_API_URL = "http://localhost:8001";

export type GatewayApiRequestInit = RequestInit & {
  retryOnUnauthorized?: boolean;
};

export class GatewayApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: Record<string, unknown>;
  readonly requestId?: string;

  constructor(
    status: number,
    code: string,
    message: string,
    details?: Record<string, unknown>,
    requestId?: string,
  ) {
    super(message);
    this.name = "GatewayApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

/** The OpenAI-compatible base URL clients call (Backend AI's /v1). */
export function gatewayOpenAIBaseUrl(): string {
  return (process.env.NEXT_PUBLIC_AI_API_URL ?? DEFAULT_AI_API_URL).replace(/\/$/, "") + "/v1";
}

function apiUrl(path: string, method = "GET"): string {
  if (/^https?:\/\//.test(path)) return path;
  const pathname = path.split("?", 1)[0].replace(/\/+$/, "");
  const isAi = pathname.startsWith("/api/v1/runtime/")
    || ["/api/v1/chat", "/api/v1/status", "/api/v1/gateway/analyze"].includes(pathname)
    || (pathname === "/api/v1/gateway/requests" && method.toUpperCase() === "POST")
    || (pathname === "/api/v1/gateway/comparisons" && method.toUpperCase() === "POST");
  const baseUrl = isAi
    ? process.env.NEXT_PUBLIC_AI_API_URL ?? DEFAULT_AI_API_URL
    : process.env.NEXT_PUBLIC_API_URL ?? DEFAULT_API_URL;
  return baseUrl.replace(/\/$/, "") + "/" + path.replace(/^\//, "");
}

function headersToRecord(headers: HeadersInit | undefined): Record<string, string> {
  const normalized = new Headers(headers);
  const result: Record<string, string> = {};
  normalized.forEach((value, key) => {
    result[key] = value;
  });
  return result;
}

function statusMessage(status: number): { code: string; message: string } {
  switch (status) {
    case 401:
      return { code: "authentication_required", message: "Your session has expired." };
    case 403:
      return {
        code: "forbidden",
        message: "You do not have permission to perform this action.",
      };
    case 404:
      return { code: "not_found", message: "The requested resource was not found." };
    case 409:
      return { code: "conflict", message: "The resource was changed by another request." };
    case 422:
      return { code: "validation_error", message: "The request payload is invalid." };
    case 503:
      return { code: "service_unavailable", message: "The service is temporarily unavailable." };
    default:
      return { code: "api_error", message: "The request could not be completed." };
  }
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object"
    ? (value as Record<string, unknown>)
    : undefined;
}

function errorFromResponse(status: number, payload: unknown): GatewayApiError {
  const fallback = statusMessage(status);
  const record = asRecord(payload);
  if (status === 403 && (
    record?.code === "APPLICATION_INACTIVE" || record?.detail === "Application is inactive"
  )) {
    return new GatewayApiError(
      status,
      "APPLICATION_INACTIVE",
      "This application is inactive. Activate it before sending requests.",
    );
  }
  const code = typeof record?.code === "string" ? record.code : fallback.code;
  const message =
    typeof record?.message === "string" && status !== 403
      ? record.message
      : typeof record?.detail === "string" && status !== 403
        ? record.detail
        : fallback.message;
  const details = asRecord(record?.details) ?? undefined;
  const requestId =
    typeof record?.request_id === "string" ? record.request_id : undefined;
  return new GatewayApiError(status, code, message, details, requestId);
}

async function fetchWithToken<T>(
  url: string,
  init: GatewayApiRequestInit,
  token: string,
): Promise<{ response: Response; payload: T | undefined }> {
  const requestInit = { ...init };
  delete requestInit.retryOnUnauthorized;
  const headers = {
    Accept: "application/json",
    ...headersToRecord(requestInit.headers),
    Authorization: "Bearer " + token,
  };
  const response = await fetch(url, { ...requestInit, headers });
  const payload = (await readJson(response)) as T | undefined;
  return { response, payload };
}

export async function apiFetch<T>(
  path: string,
  init: GatewayApiRequestInit = {},
): Promise<T> {
  const supabase = createClient();
  const sessionResult = await supabase.auth.getSession();
  let token = sessionResult.data.session?.access_token;
  if (sessionResult.error || !token) {
    throw new GatewayApiError(
      401,
      "authentication_required",
      "Your session has expired.",
    );
  }

  const url = apiUrl(path, init.method);
  let result = await fetchWithToken<T>(url, init, token);
  const shouldRetry = init.retryOnUnauthorized !== false;

  if (result.response.status === 401 && shouldRetry) {
    const refreshResult = await supabase.auth.refreshSession();
    token = refreshResult.data.session?.access_token;
    if (!refreshResult.error && token) {
      result = await fetchWithToken<T>(url, init, token);
    }
  }

  if (!result.response.ok) {
    throw errorFromResponse(result.response.status, result.payload);
  }
  return result.payload as T;
}

function queryString(params: Record<string, unknown>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    search.set(key, Array.isArray(value) ? value.join(",") : String(value));
  });
  const encoded = search.toString();
  return encoded ? "?" + encoded : "";
}

export function listApplications(
  params: ApplicationListParams = {},
): Promise<ApplicationListResponse> {
  return apiFetch<ApplicationListResponse>(
    "/api/v1/applications" + queryString(params),
  );
}

export function getApplication(
  applicationId: string,
): Promise<ApplicationDetail> {
  return apiFetch<ApplicationDetail>(
    "/api/v1/applications/" + encodeURIComponent(applicationId),
  );
}

export function listApplicationModels(applicationId: string): Promise<ApplicationModelsResponse> {
  return apiFetch<ApplicationModelsResponse>(
    "/api/v1/applications/" + encodeURIComponent(applicationId) + "/models",
  );
}

export function createApplication(input: ApplicationCreateInput): Promise<ApplicationDetail> {
  return apiFetch<ApplicationDetail>("/api/v1/applications", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function updateApplication(
  applicationId: string,
  input: ApplicationUpdateInput,
): Promise<ApplicationDetail> {
  return apiFetch<ApplicationDetail>(
    "/api/v1/applications/" + encodeURIComponent(applicationId),
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    },
  );
}

export function listApplicationKeys(applicationId: string, params: { page?: number; page_size?: number } = {}): Promise<ApplicationKeyPage> {
  return apiFetch<ApplicationKeyPage>("/api/v1/applications/" + encodeURIComponent(applicationId) + "/api-keys" + queryString(params));
}

export function createApplicationKey(applicationId: string, input: ApplicationKeyCreateInput): Promise<ApplicationKeyCreated> {
  return apiFetch<ApplicationKeyCreated>("/api/v1/applications/" + encodeURIComponent(applicationId) + "/api-keys", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
}

export function revokeApplicationKey(applicationId: string, keyId: string): Promise<ApplicationKey> {
  return apiFetch<ApplicationKey>("/api/v1/applications/" + encodeURIComponent(applicationId) + "/api-keys/" + encodeURIComponent(keyId) + "/revoke", { method: "POST" });
}

export function deleteApplication(applicationId: string): Promise<void> {
  return apiFetch<void>(
    "/api/v1/applications/" + encodeURIComponent(applicationId),
    { method: "DELETE" },
  );
}

export function getGatewayConfig(applicationId: string): Promise<GatewayConfig> {
  return apiFetch<GatewayConfig>(
    "/api/v1/applications/" +
      encodeURIComponent(applicationId) +
      "/gateway-config",
  );
}

export function updateGatewayConfig(
  applicationId: string,
  input: GatewayConfigMutationInput,
): Promise<GatewayConfig> {
  return apiFetch<GatewayConfig>(
    "/api/v1/applications/" +
      encodeURIComponent(applicationId) +
      "/gateway-config",
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    },
  );
}

export function listAvailableModels(
  params: ModelListParams = {},
): Promise<ModelListResponse> {
  return apiFetch<ModelListResponse>(
    "/api/v1/gateway/available-models" + queryString(params),
  );
}

export function listAdminModels(params: ModelListParams = {}): Promise<ModelListResponse> {
  return apiFetch<ModelListResponse>("/api/v1/gateway/models" + queryString(params));
}

export function listProviders(): Promise<ProviderSummary[]> {
  return apiFetch<ProviderSummary[]>("/api/v1/gateway/providers");
}

export function createModel(input: ModelCreateInput): Promise<ModelSummary> {
  return apiFetch<ModelSummary>("/api/v1/gateway/models", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function probeModel(modelId: string): Promise<ModelProbeResponse> {
  return apiFetch<ModelProbeResponse>(
    "/api/v1/gateway/models/" + encodeURIComponent(modelId) + "/probe",
    { method: "POST" },
  );
}

export function updateModel(modelId: string, input: ModelUpdateInput): Promise<ModelSummary> {
  return apiFetch<ModelSummary>("/api/v1/gateway/models/" + encodeURIComponent(modelId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function listProviderConnections(): Promise<ProviderConnection[]> {
  return apiFetch<ProviderConnection[]>("/api/v1/gateway/provider-connections");
}

export function createProviderConnection(input: ProviderConnectionCreateInput): Promise<ProviderConnection> {
  return apiFetch<ProviderConnection>("/api/v1/gateway/provider-connections", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function updateProviderConnection(providerId: string, input: ProviderConnectionUpdateInput): Promise<ProviderConnection> {
  return apiFetch<ProviderConnection>("/api/v1/gateway/provider-connections/" + encodeURIComponent(providerId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function replaceProviderCredential(providerId: string, input: ProviderCredentialReplaceInput): Promise<ProviderConnection> {
  return apiFetch<ProviderConnection>("/api/v1/gateway/provider-connections/" + encodeURIComponent(providerId) + "/credential", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function revokeProviderCredential(providerId: string, expectedVersion: number): Promise<void> {
  return apiFetch<void>("/api/v1/gateway/provider-connections/" + encodeURIComponent(providerId) + "/credential" + queryString({ expected_version: expectedVersion }), {
    method: "DELETE",
  });
}

export function probeProviderConnection(providerId: string, input: ProviderConnectionProbeInput): Promise<ProviderConnectionProbeResult> {
  return apiFetch<ProviderConnectionProbeResult>("/api/v1/gateway/provider-connections/" + encodeURIComponent(providerId) + "/probe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

const MY_ENDPOINTS = "/api/v1/gateway/my-endpoints";

export function listMyEndpoints(): Promise<CustomerEndpoint[]> {
  return apiFetch<CustomerEndpoint[]>(MY_ENDPOINTS);
}

export function createMyEndpoint(input: CustomerEndpointCreateInput): Promise<CustomerEndpoint> {
  return apiFetch<CustomerEndpoint>(MY_ENDPOINTS, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function deleteMyEndpoint(endpointId: string): Promise<void> {
  return apiFetch<void>(MY_ENDPOINTS + "/" + encodeURIComponent(endpointId), { method: "DELETE" });
}

export function addMyEndpointModel(endpointId: string, input: EndpointModelCreateInput): Promise<CustomerEndpoint> {
  return apiFetch<CustomerEndpoint>(MY_ENDPOINTS + "/" + encodeURIComponent(endpointId) + "/models", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

/** Model names the server itself lists at GET {base_url}/models, to pick from instead of typing. */
export function discoverMyEndpointModels(endpointId: string): Promise<string[]> {
  return apiFetch<string[]>(MY_ENDPOINTS + "/" + encodeURIComponent(endpointId) + "/discover");
}

export function probeMyEndpointModel(endpointId: string, modelId: string): Promise<CustomerEndpoint> {
  return apiFetch<CustomerEndpoint>(
    MY_ENDPOINTS + "/" + encodeURIComponent(endpointId) + "/models/" + encodeURIComponent(modelId) + "/probe",
    { method: "POST" },
  );
}

const OFFLINE_PIPELINES = "/api/v1/gateway/offline-pipelines";

export function listOfflinePipelines(): Promise<OfflinePipeline[]> {
  return apiFetch<OfflinePipeline[]>(OFFLINE_PIPELINES);
}

export function getOfflinePipeline(pipelineId: string): Promise<OfflinePipeline> {
  return apiFetch<OfflinePipeline>(OFFLINE_PIPELINES + "/" + encodeURIComponent(pipelineId));
}

export function createOfflinePipeline(input: OfflinePipelineCreateInput): Promise<OfflinePipeline> {
  return apiFetch<OfflinePipeline>(OFFLINE_PIPELINES, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function updateOfflinePipeline(pipelineId: string, input: OfflinePipelineUpdateInput): Promise<OfflinePipeline> {
  return apiFetch<OfflinePipeline>(OFFLINE_PIPELINES + "/" + encodeURIComponent(pipelineId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function deleteOfflinePipeline(pipelineId: string): Promise<void> {
  return apiFetch<void>(OFFLINE_PIPELINES + "/" + encodeURIComponent(pipelineId), { method: "DELETE" });
}

export function deleteMyEndpointModel(endpointId: string, modelId: string): Promise<CustomerEndpoint> {
  return apiFetch<CustomerEndpoint>(
    MY_ENDPOINTS + "/" + encodeURIComponent(endpointId) + "/models/" + encodeURIComponent(modelId),
    { method: "DELETE" },
  );
}

export function listServiceProviderBindings(): Promise<ServiceProviderBinding[]> {
  return apiFetch<ServiceProviderBinding[]>("/api/v1/gateway/service-provider-bindings");
}

export function setServiceProviderBinding(purpose: string, input: ServiceProviderBindingUpdateInput): Promise<ServiceProviderBinding[]> {
  return apiFetch<ServiceProviderBinding[]>("/api/v1/gateway/service-provider-bindings/" + encodeURIComponent(purpose), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function listModelPools(): Promise<ModelPool[]> {
  return apiFetch<ModelPool[]>("/api/v1/gateway/model-pools");
}

export function suggestPoolFallbacks(modelIds: string[], allowOtherProviders: boolean): Promise<FallbackSuggestion[]> {
  return apiFetch<FallbackSuggestion[]>("/api/v1/gateway/model-pools/fallback-suggestions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_ids: modelIds, allow_other_providers: allowOtherProviders }),
  });
}

export function getModelPool(poolId: string): Promise<ModelPool> {
  return apiFetch<ModelPool>("/api/v1/gateway/model-pools/" + encodeURIComponent(poolId));
}

export function createModelPool(input: ModelPoolMutationInput): Promise<ModelPool> {
  return apiFetch<ModelPool>("/api/v1/gateway/model-pools", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function updateModelPool(poolId: string, input: ModelPoolMutationInput): Promise<ModelPool> {
  return apiFetch<ModelPool>("/api/v1/gateway/model-pools/" + encodeURIComponent(poolId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function deleteModelPool(poolId: string): Promise<void> {
  return apiFetch<void>("/api/v1/gateway/model-pools/" + encodeURIComponent(poolId), {
    method: "DELETE",
  });
}

export function createGatewayRequest(
  input: GatewayRequestInput,
  requestInit: Pick<RequestInit, "signal"> = {},
): Promise<GatewayRequestResponse> {
  return apiFetch<GatewayRequestResponse>("/api/v1/gateway/requests", {
    ...requestInit,
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function listGatewayRequests(
  params: GatewayRequestListParams = {},
): Promise<GatewayRequestPage> {
  return apiFetch<GatewayRequestPage>(
    "/api/v1/gateway/requests" + queryString(params),
  );
}

export function getGatewayRequest(requestId: string): Promise<GatewayRequestDetail> {
  return apiFetch<GatewayRequestDetail>(
    "/api/v1/gateway/requests/" + encodeURIComponent(requestId),
  );
}

export function createGatewayComparison(input: ComparisonCreateInput): Promise<ComparisonAccepted> {
  return apiFetch<ComparisonAccepted>("/api/v1/gateway/comparisons", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function getGatewayComparison(
  comparisonId: string,
  requestInit: Pick<RequestInit, "signal"> = {},
): Promise<ComparisonRun> {
  return apiFetch<ComparisonRun>(
    "/api/v1/gateway/comparisons/" + encodeURIComponent(comparisonId),
    requestInit,
  );
}

export function getGatewayComparisonReport(
  params: { application_id?: string } = {},
): Promise<ComparisonReport> {
  return apiFetch<ComparisonReport>(
    "/api/v1/gateway/comparisons/report/summary" + queryString(params),
  );
}

export async function exportGatewayComparisonsCsv(
  params: { application_id?: string } = {},
): Promise<Blob> {
  const supabase = createClient();
  const sessionResult = await supabase.auth.getSession();
  let token = sessionResult.data.session?.access_token;
  if (sessionResult.error || !token) {
    throw new GatewayApiError(401, "authentication_required", "Your session has expired.");
  }
  const url = apiUrl("/api/v1/gateway/comparisons/export.csv" + queryString(params));
  const request = () => fetch(url, {
    headers: { Accept: "text/csv", Authorization: "Bearer " + token },
  });
  let response = await request();
  if (response.status === 401) {
    const refreshResult = await supabase.auth.refreshSession();
    token = refreshResult.data.session?.access_token;
    if (!refreshResult.error && token) response = await request();
  }
  if (!response.ok) throw errorFromResponse(response.status, await readJson(response));
  return response.blob();
}

export function getGatewayRequestTraces(requestId: string, params: { page?: number; page_size?: number } = {}): Promise<PageResponse<Record<string, unknown>>> {
  return apiFetch<PageResponse<Record<string, unknown>>>(
    "/api/v1/gateway/requests/" + encodeURIComponent(requestId) + "/traces" + queryString(params),
  );
}

export function getGatewayRequestExecutions(requestId: string, params: { page?: number; page_size?: number } = {}): Promise<PageResponse<Record<string, unknown>>> {
  return apiFetch<PageResponse<Record<string, unknown>>>(
    "/api/v1/gateway/requests/" + encodeURIComponent(requestId) + "/executions" + queryString(params),
  );
}

export async function exportUsageCsv(params: UsageParams = {}): Promise<Blob> {
  const supabase = createClient();
  const sessionResult = await supabase.auth.getSession();
  let token = sessionResult.data.session?.access_token;
  if (sessionResult.error || !token) {
    throw new GatewayApiError(401, "authentication_required", "Your session has expired.");
  }
  const url = apiUrl("/api/v1/gateway/usage/export.csv" + queryString(params));
  const request = () => fetch(url, {
    headers: { Accept: "text/csv", Authorization: "Bearer " + token },
  });
  let response = await request();
  if (response.status === 401) {
    const refreshResult = await supabase.auth.refreshSession();
    token = refreshResult.data.session?.access_token;
    if (!refreshResult.error && token) response = await request();
  }
  if (!response.ok) throw errorFromResponse(response.status, await readJson(response));
  return response.blob();
}

export function getOverview(params: OverviewParams = {}): Promise<OverviewResponse> {
  return apiFetch<OverviewResponse>(
    "/api/v1/gateway/overview" + queryString(params),
  );
}

export function getUsage(params: UsageParams = {}): Promise<UsageResponse> {
  return apiFetch<UsageResponse>(
    "/api/v1/gateway/usage" + queryString(params),
  );
}

export function getBillingAccount(): Promise<BillingAccount> {
  return apiFetch<BillingAccount>("/api/v1/billing/account");
}

export function getBillingTransactions(params: { page?: number; page_size?: number } = {}): Promise<BillingTransactionPage> {
  return apiFetch<BillingTransactionPage>("/api/v1/billing/transactions" + queryString(params));
}

export function getBillingSettings(): Promise<BillingSettings> {
  return apiFetch<BillingSettings>("/api/v1/billing/settings");
}

export function updateBillingSettings(input: BillingSettingsInput): Promise<BillingSettings> {
  return apiFetch<BillingSettings>("/api/v1/billing/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
}

export function createBillingTopup(input: BillingTopupInput): Promise<BillingTopupResponse> {
  return apiFetch<BillingTopupResponse>("/api/v1/billing/topups", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
}

export function listIncidents(params: IncidentParams = {}): Promise<PageResponse<Incident>> {
  return apiFetch<PageResponse<Incident>>("/api/v1/gateway/incidents" + queryString(params));
}

export function listIncidentReports(params: IncidentParams = {}): Promise<PageResponse<Incident>> {
  return apiFetch<PageResponse<Incident>>("/api/v1/gateway/incident-reports" + queryString(params));
}

export function reportIncident(input: IncidentReportInput): Promise<Incident> {
  return apiFetch<Incident>("/api/v1/gateway/incident-reports", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
}

export function createIncident(input: IncidentCreateInput): Promise<Incident> {
  return apiFetch<Incident>("/api/v1/gateway/incidents", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
}

export function updateIncident(incidentId: string, input: IncidentUpdateInput): Promise<Incident> {
  return apiFetch<Incident>("/api/v1/gateway/incidents/" + encodeURIComponent(incidentId), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
}

export function listPolicyReviews(params: PolicyReviewParams = {}): Promise<PageResponse<PolicyReview>> {
  return apiFetch<PageResponse<PolicyReview>>("/api/v1/policy-reviews" + queryString(params));
}

export function decidePolicyReview(reviewId: string, input: PolicyDecisionInput): Promise<PolicyReview> {
  return apiFetch<PolicyReview>("/api/v1/policy-reviews/" + encodeURIComponent(reviewId) + "/decision", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
}

export type { ApiErrorPayload };
