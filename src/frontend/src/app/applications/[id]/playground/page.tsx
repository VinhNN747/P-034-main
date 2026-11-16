import { GatewayWorkspace } from "@/components/gateway/gateway-workspace";

export default async function PlaygroundPage({ params }: PageProps<"/applications/[id]/playground">) {
  const { id } = await params;
  return <GatewayWorkspace route={`/applications/${id}/playground`} applicationId={id} />;
}
