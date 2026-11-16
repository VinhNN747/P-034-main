import { GatewayWorkspace } from "@/components/gateway/gateway-workspace";

export default async function ApplicationSettingsPage({ params }: PageProps<"/applications/[id]/settings">) {
  const { id } = await params;
  return <GatewayWorkspace route={`/applications/${id}/settings`} applicationId={id} />;
}
