// fallbackHref: where the item links when its own page is restricted but the fallback is not.
export type NavigationItem = { href: string; label: string; group: "Workspace" | "Operate"; anyOf?: string[]; requires?: string[]; userOnly?: boolean; fallbackHref?: string };

const navigation: NavigationItem[] = [
  { href: "/dashboard", label: "Overview", group: "Workspace" },
  { href: "/sdk-guide", label: "SDK Guide", group: "Workspace", userOnly: true },
  { href: "/applications", label: "Applications", group: "Workspace", anyOf: ["applications.read_own", "applications.read_global"] },
  // Offline pipelines and My endpoints share one section; its pages switch between them with tabs.
  { href: "/offline", label: "Offline", group: "Workspace", anyOf: ["applications.read_own", "applications.read_global", "model_pools.read_own"], fallbackHref: "/my-endpoints" },
  { href: "/models", label: "Models", group: "Operate", anyOf: ["models.read", "models.read_available"] },
  { href: "/providers", label: "Providers", group: "Operate", requires: ["models.manage"] },
  { href: "/model-pools", label: "Model pools", group: "Workspace", requires: ["model_pools.read_own"] },
  { href: "/requests", label: "Requests", group: "Operate", anyOf: ["requests.read_own", "requests.read_global", "usage.read_own", "usage.read_global"] },
  { href: "/usage", label: "Usage", group: "Operate", anyOf: ["usage.read_own", "usage.read_global"] },
  { href: "/billing", label: "Billing", group: "Operate", requires: ["billing.read"] },
  { href: "/incidents", label: "Incidents", group: "Operate", anyOf: ["incidents.read", "incidents.manage"] },
];

type RouteRule = { pattern: RegExp; anyOf?: string[]; requires?: string[]; userOnly?: boolean };
const routeRules: RouteRule[] = [
  { pattern: /^\/dashboard$/ },
  { pattern: /^\/reports$/, userOnly: true },
  { pattern: /^\/reports\/new$/, userOnly: true },
  { pattern: /^\/sdk-guide$/, userOnly: true },
  { pattern: /^\/applications$/, anyOf: ["applications.read_own", "applications.read_global"] },
  { pattern: /^\/applications\/new$/, anyOf: ["applications.create", "applications.manage_own"] },
  { pattern: /^\/applications\/[^/]+$/, anyOf: ["applications.read_own", "applications.read_global"] },
  { pattern: /^\/applications\/[^/]+\/playground$/, anyOf: ["applications.read_own", "applications.manage_own"] },
  { pattern: /^\/applications\/[^/]+\/settings$/, anyOf: ["applications.update_own", "applications.manage_own"] },
  { pattern: /^\/offline$/, anyOf: ["applications.read_own", "applications.read_global"] },
  // Building a pipeline picks models from the user's endpoints, so it also needs the pool right.
  { pattern: /^\/offline\/new$/, requires: ["applications.manage_own", "model_pools.manage_own"] },
  { pattern: /^\/offline\/[^/]+$/, anyOf: ["applications.read_own", "applications.read_global"] },
  { pattern: /^\/offline\/[^/]+\/playground$/, anyOf: ["applications.read_own", "applications.manage_own"] },
  { pattern: /^\/offline\/[^/]+\/settings$/, requires: ["applications.manage_own", "model_pools.manage_own"] },
  { pattern: /^\/available-models$/, anyOf: ["models.read", "models.read_available"] },
  { pattern: /^\/models$/, anyOf: ["models.read", "models.read_available"] },
  { pattern: /^\/models\/new$/, requires: ["models.manage"] },
  { pattern: /^\/providers$/, requires: ["models.manage"] },
  { pattern: /^\/model-pools$/, requires: ["model_pools.read_own"] },
  { pattern: /^\/model-pools\/new$/, requires: ["model_pools.manage_own"] },
  { pattern: /^\/model-pools\/[^/]+$/, requires: ["model_pools.manage_own"] },
  { pattern: /^\/my-endpoints$/, requires: ["model_pools.read_own"] },
  { pattern: /^\/requests$/, anyOf: ["requests.read_own", "requests.read_global", "usage.read_own", "usage.read_global"] },
  { pattern: /^\/requests\/[^/]+$/, anyOf: ["requests.read_own", "requests.read_global", "usage.read_own", "usage.read_global"] },
  { pattern: /^\/usage$/, anyOf: ["usage.read_own", "usage.read_global"] },
  { pattern: /^\/billing$/, requires: ["billing.read"] },
  { pattern: /^\/incidents$/, anyOf: ["incidents.read", "incidents.manage"] },
  { pattern: /^\/incidents\/new$/, requires: ["incidents.manage"] },
  { pattern: /^\/policy-reviews$/, requires: ["routing_policies.read"] },
];

export const can = (permissions: readonly string[], required: readonly string[] = []) => required.every((permission) => permissions.includes(permission));
export const canAny = (permissions: readonly string[], candidates: readonly string[] = []) => candidates.length === 0 || candidates.some((permission) => permissions.includes(permission));
const matchesRule = (permissions: readonly string[], rule: Pick<RouteRule, "anyOf" | "requires">) => can(permissions, rule.requires) && canAny(permissions, rule.anyOf);
const isNonAdminUser = (roles: readonly string[]) => roles.includes("user") && !roles.includes("admin") && !roles.includes("platform_admin");
export const getNavigation = (permissions: readonly string[], roles: readonly string[] = []) => navigation
  .filter((item) => matchesRule(permissions, item) && (!item.userOnly || isNonAdminUser(roles)))
  .map((item) => item.fallbackHref && getRouteViewState(item.href, permissions, roles) === "restricted" ? { ...item, href: item.fallbackHref } : item);
export function getRouteViewState(pathname: string, permissions: readonly string[], roles: readonly string[] = []) {
  const rule = routeRules.find((candidate) => candidate.pattern.test(pathname));
  return rule && matchesRule(permissions, rule) && (!rule.userOnly || isNonAdminUser(roles)) ? "allowed" : "restricted";
}
