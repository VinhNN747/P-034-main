import { describe, expect, it } from "vitest";
import { canCompareRouting, comparisonIsTerminal, comparisonProgress } from "./playground-comparison";
import type { ComparisonRun, GatewayRequestDetail } from "@/lib/gateway-types";

function report(status: string, gatewayStatus: string, referenceStatus: string, evaluationStatus = "pending") {
  return {
    status, quality_evaluation: true, evaluation_status: evaluationStatus,
    branches: [
      { branch_type: "gateway", status: gatewayStatus },
      { branch_type: "reference", status: referenceStatus },
    ],
  } as ComparisonRun;
}

describe("comparison progress", () => {
  it("distinguishes the AI queue from provider execution", () => {
    expect(comparisonProgress(report("queued", "queued", "queued"))).toContain("Backend AI");
    expect(comparisonProgress(report("running", "running", "running"))).toContain("Running Gateway");
  });

  it("shows reference and evaluator progress after Gateway completes", () => {
    expect(comparisonProgress(report("running", "completed", "running"))).toContain("sequentially");
    expect(comparisonProgress(report("running", "completed", "completed", "running"))).toContain("evaluating quality");
    expect(comparisonProgress(report("completed", "completed", "completed", "failed"))).toContain("failed");
  });

  it("only reuses a completed routing receipt for the unchanged prompt", () => {
    const receipt = { status: "completed", prompt: "Say hello" } as GatewayRequestDetail;
    expect(canCompareRouting(receipt, "  Say hello  ")).toBe(true);
    expect(canCompareRouting(receipt, "Different prompt")).toBe(false);
    expect(canCompareRouting({ ...receipt, status: "failed" }, "Say hello")).toBe(false);
    expect(canCompareRouting(null, "Say hello")).toBe(false);
  });

  it("keeps polling until the comparison has a terminal receipt", () => {
    expect(comparisonIsTerminal("queued")).toBe(false);
    expect(comparisonIsTerminal("running")).toBe(false);
    for (const status of ["completed", "partial", "failed", "indeterminate"]) {
      expect(comparisonIsTerminal(status)).toBe(true);
    }
  });

  it("explains evaluator failure while preserving completed answers", () => {
    const message = comparisonProgress(report("partial", "completed", "completed", "failed"));
    expect(message).toContain("Quality evaluation failed");
    expect(message).toContain("saved answers");
  });
});
