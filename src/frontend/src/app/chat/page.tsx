import { redirect } from "next/navigation";
import { ChatWelcome } from "@/components/chat-welcome";
import { LogoutButton } from "@/components/logout-button";
import { createClient } from "@/lib/supabase/server";

export default async function ChatPage() {
  const supabase = await createClient();
  const { data } = await supabase.auth.getClaims();
  if (!data?.claims) redirect("/login");

  return <main className="chat-page"><ChatWelcome /><LogoutButton /></main>;
}
