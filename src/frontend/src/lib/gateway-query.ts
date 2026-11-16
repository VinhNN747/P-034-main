import type { GatewayApiError } from "@/lib/gateway-api";
import type {
  ApplicationListParams,
  ModelListParams,
  OverviewParams,
  UsageParams,
} from "@/lib/gateway-types";

export type QueryKey = readonly unknown[];

export type AsyncState<T> =
  | { status: "loading" }
  | { status: "success"; data: T }
  | { status: "empty"; data: T }
  | { status: "error"; error: GatewayApiError };

export function createLoadingState(): AsyncState<never> {
  return { status: "loading" };
}

export function createSuccessState<T>(data: T): AsyncState<T> {
  return { status: "success", data };
}

export function createEmptyState<T>(data: T): AsyncState<T> {
  return { status: "empty", data };
}

export function createErrorState(error: GatewayApiError): AsyncState<never> {
  return { status: "error", error };
}

function stableQueryValue(value: unknown): string {
  if (Array.isArray(value)) {
    return JSON.stringify([...value].sort());
  }
  return JSON.stringify(value);
}

function resourceKey(resource: string, params: Record<string, unknown> = {}): QueryKey {
  const normalized = Object.entries(params)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => [key, stableQueryValue(value)]);
  return ["gateway", resource, JSON.stringify(normalized)] as const;
}

export const gatewayQueryKeys = {
  all: ["gateway"] as const,
  applications: (params: ApplicationListParams = {}) =>
    resourceKey("applications", params),
  application: (applicationId: string) =>
    ["gateway", "application", applicationId] as const,
  gatewayConfig: (applicationId: string) =>
    ["gateway", "gateway-config", applicationId] as const,
  availableModels: (params: ModelListParams = {}) =>
    resourceKey("available-models", params),
  models: (params: ModelListParams = {}) => resourceKey("models", params),
  providerConnections: () => ["gateway", "provider-connections"] as const,
  serviceProviderBindings: () => ["gateway", "service-provider-bindings"] as const,
  modelPools: () => ["gateway", "model-pools"] as const,
  requests: (params: { applicationId?: string } = {}) =>
    resourceKey("requests", params),
  request: (requestId: string) =>
    ["gateway", "request", requestId] as const,
  traces: (requestId: string) =>
    ["gateway", "request-traces", requestId] as const,
  executions: (requestId: string) =>
    ["gateway", "request-executions", requestId] as const,
  overview: (params: OverviewParams = {}) => resourceKey("overview", params),
  usage: (params: UsageParams = {}) => resourceKey("usage", params),
  incidents: (params: Record<string, unknown> = {}) =>
    resourceKey("incidents", params),
  policyReviews: (params: Record<string, unknown> = {}) =>
    resourceKey("policy-reviews", params),
  billingAccount: () => ["gateway", "billing-account"] as const,
  billingTransactions: (params: Record<string, unknown> = {}) =>
    resourceKey("billing-transactions", params),
  billingSettings: () => ["gateway", "billing-settings"] as const,
};
