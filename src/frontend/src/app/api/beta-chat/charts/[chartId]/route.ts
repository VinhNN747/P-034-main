import { proxySupportChat } from "@/lib/support-chat-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request, { params }: { params: Promise<{ chartId: string }> }) {
  const { chartId } = await params;
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(chartId)) {
    return Response.json({ error: "The chart identifier is invalid." }, { status: 400 });
  }
  return proxySupportChat(request, `charts/${chartId}`, "GET");
}
