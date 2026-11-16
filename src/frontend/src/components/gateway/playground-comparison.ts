import type { ComparisonRun, GatewayRequestDetail } from "@/lib/gateway-types";

export type ComparisonProgressKey =
  | "playground.comparisonQueued"
  | "playground.comparisonIncomplete"
  | "playground.qualityEvaluationFailed"
  | "playground.runningGateway"
  | "playground.gatewayWaitingDirect"
  | "playground.gatewayRunningDirect"
  | "playground.outputsEvaluating"
  | "playground.outputsQualityFailed"
  | "playground.comparisonComplete";

const englishProgress: Record<ComparisonProgressKey, string> = {
  "playground.comparisonQueued": "Comparison queued in Backend AI",
  "playground.comparisonIncomplete": "Comparison could not complete",
  "playground.qualityEvaluationFailed": "Quality evaluation failed · saved answers and cost comparison remain available",
  "playground.runningGateway": "Running Gateway routing",
  "playground.gatewayWaitingDirect": "Gateway complete · waiting for direct models",
  "playground.gatewayRunningDirect": "Gateway complete · running direct models sequentially",
  "playground.outputsEvaluating": "Outputs ready · evaluating quality",
  "playground.outputsQualityFailed": "Outputs ready · quality evaluation failed",
  "playground.comparisonComplete": "Comparison complete",
};

export function canCompareRouting(request: GatewayRequestDetail | null, prompt: string): boolean {
  return !!request && ["completed", "partial"].includes(request.status)
    && !!request.prompt && request.prompt === prompt.trim();
}

export function comparisonIsTerminal(status: string): boolean {
  return ["completed", "partial", "failed", "indeterminate"].includes(status);
}

export function comparisonProgress(run: ComparisonRun, translate: (key: ComparisonProgressKey) => string = (key) => englishProgress[key]): string {
  if (run.status === "queued") return translate("playground.comparisonQueued");
  if (run.status === "failed" || run.status === "indeterminate") return translate("playground.comparisonIncomplete");
  if (run.quality_evaluation && run.evaluation_status === "failed") {
    return translate("playground.qualityEvaluationFailed");
  }
  const gateway = run.branches.find((branch) => branch.branch_type === "gateway");
  if (gateway?.status === "running" || gateway?.status === "queued") return translate("playground.runningGateway");
  if (run.branches.some((branch) => branch.branch_type === "reference" && ["queued", "running"].includes(branch.status))) {
    return run.execution_strategy === "parallel" ? translate("playground.gatewayWaitingDirect") : translate("playground.gatewayRunningDirect");
  }
  if (run.quality_evaluation && ["pending", "running"].includes(run.evaluation_status ?? "pending")) {
    return translate("playground.outputsEvaluating");
  }
  return run.evaluation_status === "failed" ? translate("playground.outputsQualityFailed") : translate("playground.comparisonComplete");
}
