import type { GatewayRequestInput } from "@/lib/gateway-types";

export function buildPlaygroundGatewayRequest(
  applicationId: string,
  prompt: string,
): GatewayRequestInput {
  return {
    application_id: applicationId,
    prompt: prompt.trim(),
    source: "playground",
    router: "database",
    decomposer: "llm",
    aggregator: "deterministic",
  };
}
