import { GatewayWorkspace } from "@/components/gateway/gateway-workspace";

export default async function ModelPoolPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <GatewayWorkspace route={`/model-pools/${id}`} modelPoolId={id} />;
}
