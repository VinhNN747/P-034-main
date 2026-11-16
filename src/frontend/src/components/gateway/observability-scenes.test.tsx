import { describe, expect, it } from "vitest";

import { DashboardScene, UsageScene } from "@/components/gateway/observability-scenes";
import { GatewayScene } from "@/components/gateway/gateway-scenes";

describe("observability scene routing", () => {
  it("routes dashboard and usage to live API-backed scenes", () => {
    const permissions = ["usage.read_own"];
    expect(GatewayScene({ route: "/dashboard", permissions })).toMatchObject({ type: DashboardScene });
    expect(GatewayScene({ route: "/usage", permissions })).toMatchObject({ type: UsageScene });
  });
});
