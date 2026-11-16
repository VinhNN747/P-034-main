import { describe, expect, it } from "vitest";

import { BillingScene } from "@/components/gateway/billing-scenes";
import { GatewayScene } from "@/components/gateway/gateway-scenes";

describe("billing scene routing", () => {
  it("routes billing to the API-backed ledger scene", () => {
    const permissions = ["billing.read", "billing.manage"];
    expect(GatewayScene({ route: "/billing", permissions })).toMatchObject({ type: BillingScene });
  });
});
