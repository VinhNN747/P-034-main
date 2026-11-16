import { GatewayWorkspace } from "@/components/gateway/gateway-workspace";

export default async function ApplicationPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <GatewayWorkspace route={`/applications/${id}`} applicationId={id} />;
}
