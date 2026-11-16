import { proxySupportChat } from "@/lib/support-chat-proxy";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ sessionId: string }> },
) {
  const { sessionId } = await params;
  return proxySupportChat(request, `sessions/${encodeURIComponent(sessionId)}`, "GET");
}

export async function DELETE(
  request: Request,
  { params }: { params: Promise<{ sessionId: string }> },
) {
  const { sessionId } = await params;
  return proxySupportChat(request, `sessions/${encodeURIComponent(sessionId)}`, "DELETE");
}

export async function PATCH(
  request: Request,
  { params }: { params: Promise<{ sessionId: string }> },
) {
  const { sessionId } = await params;
  return proxySupportChat(request, `sessions/${encodeURIComponent(sessionId)}`, "PATCH");
}
