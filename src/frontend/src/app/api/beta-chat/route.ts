import { proxySupportChat } from "@/lib/support-chat-proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  return proxySupportChat(request, "chat", "POST");
}
