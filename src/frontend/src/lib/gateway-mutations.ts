import {
  gatewayQueryKeys,
  type QueryKey,
} from "@/lib/gateway-query";

export type GatewayMutation =
  | {
      kind: "application.created" | "application.updated" | "application.deleted";
      applicationId?: string;
    }
  | { kind: "model_pool.deleted"; poolId?: string }
  | { kind: "gateway-config.updated"; applicationId: string }
  | { kind: "model.created" | "model.probed"; modelId?: string }
  | { kind: "model.updated"; modelId?: string; metadataChanged?: boolean }
  | { kind: "provider.created" | "provider.updated" | "provider.credential_updated" | "provider.probed"; providerId?: string }
  | { kind: "service_binding.updated"; purpose?: string }
  | {
      kind: "gateway-request.created";
      applicationId: string;
      requestId: string;
    }
  | { kind: "incident.created" | "incident.updated"; incidentId?: string }
  | { kind: "policy-review.decision"; reviewId: string }
  | { kind: "billing.topup" | "billing.settings.updated" };

export function invalidationKeysForMutation(
  mutation: GatewayMutation,
): QueryKey[] {
  switch (mutation.kind) {
    case "application.created":
    case "application.updated":
      return [
        gatewayQueryKeys.applications(),
        ...(mutation.applicationId
          ? [gatewayQueryKeys.application(mutation.applicationId)]
          : []),
      ];
    case "application.deleted":
      return [
        gatewayQueryKeys.applications(),
        ...(mutation.applicationId
          ? [
              gatewayQueryKeys.application(mutation.applicationId),
              gatewayQueryKeys.requests({ applicationId: mutation.applicationId }),
              gatewayQueryKeys.overview({ application_id: mutation.applicationId }),
              gatewayQueryKeys.usage({ application_id: mutation.applicationId }),
            ]
          : []),
      ];
    case "model_pool.deleted":
      return [gatewayQueryKeys.modelPools()];
    case "gateway-config.updated":
      return [
        gatewayQueryKeys.application(mutation.applicationId),
        gatewayQueryKeys.gatewayConfig(mutation.applicationId),
      ];
    case "model.created":
    case "model.probed":
      return [
        gatewayQueryKeys.models(),
        gatewayQueryKeys.availableModels(),
        ...(mutation.modelId
          ? [["gateway", "model", mutation.modelId] as const]
          : []),
      ];
    case "model.updated":
      return [
        gatewayQueryKeys.models(),
        gatewayQueryKeys.availableModels(),
        ...(mutation.modelId ? [["gateway", "model", mutation.modelId] as const] : []),
        ...(mutation.metadataChanged ? [gatewayQueryKeys.modelPools()] : []),
      ];
    case "provider.created":
    case "provider.updated":
    case "provider.credential_updated":
      return [
        gatewayQueryKeys.providerConnections(),
        gatewayQueryKeys.models(),
        gatewayQueryKeys.availableModels(),
        gatewayQueryKeys.modelPools(),
      ];
    case "provider.probed":
      return [gatewayQueryKeys.providerConnections()];
    case "service_binding.updated":
      return [gatewayQueryKeys.providerConnections(), gatewayQueryKeys.serviceProviderBindings()];
    case "gateway-request.created":
      return [
        gatewayQueryKeys.request(mutation.requestId),
        gatewayQueryKeys.requests({ applicationId: mutation.applicationId }),
        gatewayQueryKeys.overview({ application_id: mutation.applicationId }),
        gatewayQueryKeys.usage({ application_id: mutation.applicationId }),
      ];
    case "incident.created":
    case "incident.updated":
      return [gatewayQueryKeys.incidents()];
    case "policy-review.decision":
      return [gatewayQueryKeys.policyReviews()];
    case "billing.topup":
      return [
        gatewayQueryKeys.billingAccount(),
        gatewayQueryKeys.billingTransactions(),
      ];
    case "billing.settings.updated":
      return [gatewayQueryKeys.billingSettings()];
  }
}
