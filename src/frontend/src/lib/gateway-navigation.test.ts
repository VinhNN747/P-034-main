import { describe, expect, it } from "vitest";
import { getNavigation, getRouteViewState } from "./gateway-navigation";

describe("user incident reporting", () => {
  it("provides reporting to authenticated workspace users without incident management", () => {
    expect(getNavigation([]).some((item) => item.href === "/reports")).toBe(true);
    expect(getRouteViewState("/reports", [])).toBe("allowed");
    expect(getRouteViewState("/reports/new", [])).toBe("allowed");
    expect(getRouteViewState("/incidents", [])).toBe("restricted");
    expect(getRouteViewState("/incidents/new", [])).toBe("restricted");
    expect(getRouteViewState("/reports/unknown", [])).toBe("restricted");
    expect(getRouteViewState("/incidents", ["incidents.manage"])).toBe("allowed");
    expect(getNavigation(["incidents.manage"]).some((item) => item.href === "/incidents")).toBe(true);
  });
});

describe("private optional model pool navigation", () => {
  it("puts user model pools in the workspace and allows editing", () => {
    const permissions = ["model_pools.read_own", "model_pools.manage_own"];
    expect(getNavigation(permissions).find((item) => item.href === "/model-pools")?.group).toBe("Workspace");
    expect(getRouteViewState("/model-pools/new", permissions)).toBe("allowed");
  });
  it("does not grant pool management using legacy admin claims", () => {
    const permissions = ["model_pools.read", "model_pools.manage", "models.manage"];
    expect(getNavigation(permissions).some((item) => item.href === "/model-pools")).toBe(false);
    expect(getRouteViewState("/model-pools/new", permissions)).toBe("restricted");
  });
});

describe("request history navigation", () => {
  it("shows global request history to request or usage readers", () => {
    const permissions = ["requests.read_own"];
    expect(getNavigation(permissions).find((item) => item.href === "/requests")?.group).toBe("Operate");
    expect(getRouteViewState("/requests", permissions)).toBe("allowed");
    expect(getRouteViewState("/requests/req-1", permissions)).toBe("allowed");
    expect(getRouteViewState("/requests", ["applications.read_own"])).toBe("restricted");
  });
});

describe("provider connection administration", () => {
  it("restricts the providers page and menu to models.manage", () => {
    expect(getNavigation(["models.read"]).some((item) => item.href === "/providers")).toBe(false);
    expect(getRouteViewState("/providers", ["models.read"])).toBe("restricted");
    expect(getNavigation(["models.manage"]).some((item) => item.href === "/providers")).toBe(true);
    expect(getRouteViewState("/providers", ["models.manage"])).toBe("allowed");
  });
});
