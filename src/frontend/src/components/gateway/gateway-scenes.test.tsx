import { describe, expect, it } from "vitest";

import {
  ApplicationDetailScene,
  ApplicationSettingsScene,
  ApplicationsScene,
  NewApplicationScene,
} from "@/components/gateway/application-scenes";
import { DashboardScene } from "@/components/gateway/observability-scenes";
import { GatewayScene } from "@/components/gateway/gateway-scenes";
import { ModelPoolEditorScene, ModelPoolsScene } from "@/components/gateway/model-pool-scenes";
import { RequestDetailScene, RequestsScene } from "@/components/gateway/request-scenes";

const permissions = ["applications.read_own", "applications.manage_own"];

describe("overview scene routing", () => {
  it("keeps the dashboard entry point on the observability scene", () => {
    expect(GatewayScene({ route: "/dashboard", permissions })).toMatchObject({ type: DashboardScene });
  });
});

describe("application scene routing", () => {
  it("routes application pages to API-backed scenes", () => {
    expect(
      GatewayScene({ route: "/applications", permissions }),
    ).toMatchObject({ type: ApplicationsScene });
    expect(
      GatewayScene({ route: "/applications/new", permissions }),
    ).toMatchObject({ type: NewApplicationScene });
    expect(
      GatewayScene({ route: "/applications/app-1", applicationId: "app-1", permissions }),
    ).toMatchObject({ type: ApplicationDetailScene });
    expect(
      GatewayScene({ route: "/applications/app-1/settings", applicationId: "app-1", permissions }),
    ).toMatchObject({ type: ApplicationSettingsScene });
  });
});

describe("model pool scene routing", () => {
  const poolPermissions = ["model_pools.read_own", "model_pools.manage_own"];

  it("routes model pool list, create, and edit pages", () => {
    expect(GatewayScene({ route: "/model-pools", permissions: poolPermissions })).toMatchObject({ type: ModelPoolsScene });
    expect(GatewayScene({ route: "/model-pools/new", permissions: poolPermissions })).toMatchObject({ type: ModelPoolEditorScene });
    expect(GatewayScene({ route: "/model-pools/pool-1", modelPoolId: "pool-1", permissions: poolPermissions })).toMatchObject({ type: ModelPoolEditorScene, props: { poolId: "pool-1" } });
  });
});

describe("request history scene routing", () => {
  it("routes global history and request detail to their trace scenes", () => {
    const requestPermissions = ["requests.read_own"];
    expect(GatewayScene({ route: "/requests", permissions: requestPermissions })).toMatchObject({ type: RequestsScene });
    expect(GatewayScene({ route: "/requests/req-1", requestId: "req-1", permissions: requestPermissions })).toMatchObject({ type: RequestDetailScene, props: { id: "req-1" } });
  });
});
