"use client";

import { ApplicationDetailScene, ApplicationSettingsScene, ApplicationsScene, NewApplicationScene } from "./application-scenes";
import { BillingScene } from "./billing-scenes";
import { IncidentsScene, NewIncidentScene, PolicyReviewsScene } from "./incident-policy-scenes";
import { NewIncidentReportScene } from "./incident-report-scene";
import { AddModelScene, UnifiedModelsScene } from "./model-scenes";
import { ProviderConnectionsScene } from "./provider-scenes";
import { CustomerEndpointsScene } from "./endpoint-scenes";
import { ModelPoolEditorScene, ModelPoolsScene } from "./model-pool-scenes";
import { OfflinePipelineDetailScene, OfflinePipelineEditorScene, OfflinePipelinesScene } from "./offline-pipeline-scenes";
import { DashboardScene, UsageScene } from "./observability-scenes";
import { RequestDetailScene, RequestsScene } from "./request-scenes";
import { PlaygroundScene } from "./playground-scene";
import { SdkGuideScene } from "./sdk-guide-scene";

export type SceneProps = { route: string; applicationId?: string; modelPoolId?: string; requestId?: string; permissions: readonly string[] };

export function GatewayScene({ route, applicationId, modelPoolId, requestId, permissions }: SceneProps) {
  if (route === "/dashboard") return <DashboardScene permissions={permissions} />;
  if (route === "/sdk-guide") return <SdkGuideScene />;
  if (route === "/applications") return <ApplicationsScene permissions={permissions} />;
  if (route === "/applications/new") return <NewApplicationScene permissions={permissions} />;
  // Offline pipelines before the generic /settings rule; their playground is the shared PlaygroundScene.
  if (route === "/offline") return <OfflinePipelinesScene permissions={permissions} />;
  if (route === "/offline/new") return <OfflinePipelineEditorScene permissions={permissions} />;
  if (/^\/offline\/[^/]+\/settings$/.test(route)) return <OfflinePipelineEditorScene pipelineId={applicationId} permissions={permissions} />;
  if (/^\/offline\/[^/]+$/.test(route)) return <OfflinePipelineDetailScene id={applicationId} permissions={permissions} />;
  if (route.endsWith("/playground")) return <PlaygroundScene id={applicationId} />;
  if (route.endsWith("/settings")) return <ApplicationSettingsScene id={applicationId} permissions={permissions} />;
  if (/^\/applications\/[^/]+$/.test(route)) return <ApplicationDetailScene id={applicationId} permissions={permissions} />;
  if (route === "/available-models" || route === "/models") return <UnifiedModelsScene permissions={permissions} />;
  if (route === "/models/new") return <AddModelScene />;
  if (route === "/providers") return <ProviderConnectionsScene permissions={permissions} />;
  if (route === "/my-endpoints") return <CustomerEndpointsScene permissions={permissions} />;
  if (route === "/model-pools") return <ModelPoolsScene permissions={permissions} />;
  if (route === "/model-pools/new") return <ModelPoolEditorScene permissions={permissions} />;
  if (/^\/model-pools\/[^/]+$/.test(route)) return <ModelPoolEditorScene poolId={modelPoolId} permissions={permissions} />;
  if (route === "/requests") return <RequestsScene permissions={permissions} />;
  if (/^\/requests\/[^/]+$/.test(route)) return <RequestDetailScene id={requestId} />;
  if (route === "/usage") return <UsageScene permissions={permissions} />;
  if (route === "/billing") return <BillingScene permissions={permissions} />;
  if (route === "/incidents") return <IncidentsScene permissions={permissions} />;
  if (route === "/reports") return <IncidentsScene permissions={permissions} reportsOnly />;
  if (route === "/reports/new") return <NewIncidentReportScene permissions={permissions} />;
  if (route === "/incidents/new") return <NewIncidentScene />;
  if (route === "/policy-reviews") return <PolicyReviewsScene permissions={permissions} />;
  return null;
}
