import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  IncidentsScene,
  IncidentTicket,
  NewIncidentScene,
  PolicyReviewsScene,
} from "@/components/gateway/incident-policy-scenes";
import { GatewayScene } from "@/components/gateway/gateway-scenes";
import { NewIncidentReportScene } from "@/components/gateway/incident-report-scene";
import type { Incident } from "@/lib/gateway-types";

describe("incident and policy review scene routing", () => {
  it("routes incidents and policy reviews to API-backed scenes", () => {
    const permissions = ["incidents.read", "incidents.manage", "routing_policies.read", "routing_policies.manage"];
    expect(GatewayScene({ route: "/incidents", permissions })).toMatchObject({ type: IncidentsScene });
    expect(GatewayScene({ route: "/incidents/new", permissions })).toMatchObject({ type: NewIncidentScene });
    expect(GatewayScene({ route: "/policy-reviews", permissions })).toMatchObject({ type: PolicyReviewsScene });
  });

  it("routes user report history and submission without admin permissions", () => {
    expect(GatewayScene({ route: "/reports", permissions: [] })).toMatchObject({ type: IncidentsScene, props: { reportsOnly: true } });
    expect(GatewayScene({ route: "/reports/new", permissions: [] })).toMatchObject({ type: NewIncidentReportScene });
    const html = renderToStaticMarkup(createElement(NewIncidentReportScene, { permissions: [] }));
    expect(html).toContain('value="fallback"');
    expect(html).toContain('value="logic"');
    expect(html).toContain('maxLength="10000"');
    expect(html).not.toContain('value="resolved"');
    expect(html).not.toContain('value="critical"');
  });
});

describe("incident ticket details", () => {
  const incident: Incident = {
    id: "ticket-1", title: "Fallback failed", description: "<script>unsafe()</script>\nRequest stopped",
    severity: "medium", status: "investigating", owner: "Unassigned", type: "fallback", scope: "app-1",
    opened_at: "2026-10-12T09:00:00Z", updated_at: "2026-10-12T09:00:00Z", duration_seconds: 0,
    impact: "Request failed", source: "user_report", reporter_email: "reporter@example.com",
    request_id: "request-1", steps_to_reproduce: "Disable primary model", expected_behavior: "Use fallback",
    resolution_note: "Routing fix applied",
  };

  it("shows the description and handling note to the reporter without management fields", () => {
    const html = renderToStaticMarkup(createElement(IncidentTicket, { incident, canManage: false, onUpdated: () => undefined }));
    expect(html).toContain("Routing fix applied");
    expect(html).toContain("Disable primary model");
    expect(html).toContain("request-1");
    expect(html).toContain("reporter@example.com");
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("<form");
  });

  it("lets managers assign severity, status, owner and a handling note", () => {
    const html = renderToStaticMarkup(createElement(IncidentTicket, { incident, canManage: true, onUpdated: () => undefined }));
    expect(html).toContain("<form");
    expect(html).toContain('value="resolved"');
    expect(html).toContain('value="critical"');
    expect(html).toContain('value="Unassigned"');
    expect(html).toContain("Routing fix applied</textarea>");
  });
});
