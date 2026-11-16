import { GatewayWorkspace } from "@/components/gateway/gateway-workspace";

export default async function OfflinePipelineSettingsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <GatewayWorkspace route={`/offline/${id}/settings`} applicationId={id} />;
}
