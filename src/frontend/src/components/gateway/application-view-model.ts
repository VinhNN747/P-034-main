import type { ApplicationSummary, ModelSummary } from "@/lib/gateway-types";

export type ApplicationViewModel = {
  id: string;
  name: string;
  slug: string;
  environment: string;
  status: string;
  objective: string;
  models: string[];
  modelTotal: number;
  budget: string;
  requestCount: number;
  configVersion: number | null;
  budgetSummary: ApplicationSummary["budget"];
};

const objectiveLabels: Record<string, string> = {
  balanced: "Balanced",
  cost_first: "Lowest cost",
  quality_first: "Highest quality",
  latency_first: "Lowest latency",
};

export function toApplicationViewModel(
  application: ApplicationSummary,
  modelCatalog: ModelSummary[] = [],
): ApplicationViewModel {
  const config = application.gateway_config;
  const modelNames = new Map<string, string>();
  modelCatalog.forEach((model) => {
    const label = model.display_name || model.name;
    modelNames.set(model.id, label);
    modelNames.set(model.name, label);
  });
  const models = application.model_preview?.length
    ? application.model_preview.map((model) => model.display_name ?? model.name)
    : config?.model_ids?.length
      ? config.model_ids.map((modelId) => modelNames.get(modelId) ?? modelId)
      : application.models?.map((model) => model.display_name ?? model.name) ?? [];
  return {
    id: application.id,
    name: application.name,
    slug: application.slug,
    environment: application.environment,
    status: application.status,
    objective: objectiveLabels[application.routing_objective ?? config?.routing_objective ?? "balanced"] ??
      application.routing_objective ?? config?.routing_objective ??
      "Balanced",
    models,
    modelTotal: application.model_total ?? models.length,
    budget:
      application.budget_ceiling === undefined && (config?.budget_ceiling === undefined || config.budget_ceiling === null)
        ? "Not set"
        : (application.budget_ceiling ?? config?.budget_ceiling)?.toFixed(2) ?? "Not set",
    requestCount: application.request_count ?? 0,
    configVersion: application.config_version ?? config?.version ?? null,
    budgetSummary: application.budget ?? null,
  };
}
