import { GatewayWorkspace } from "@/components/gateway/gateway-workspace";

export default async function RequestDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <GatewayWorkspace route={`/requests/${id}`} requestId={id} />;
}
