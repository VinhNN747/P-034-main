import { proxySupportChat } from "@/lib/support-chat-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return proxySupportChat(request, "sessions", "GET");
}

export async function POST(request: Request) {
  return proxySupportChat(request, "sessions", "POST");
}
