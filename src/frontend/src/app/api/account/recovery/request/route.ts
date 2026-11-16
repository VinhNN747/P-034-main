import { accountResponse, readAccountInput } from "@/lib/account-security";

export async function POST(request: Request) {
  const input = await readAccountInput(request);
  if (!input || typeof input.email !== "string" || input.email.length > 254) return accountResponse({ code: "invalidRequest" }, 400);
  const backendUrl = process.env.BACKEND_API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
  const redirectTo = new URL("/auth/confirm", request.url).toString();
  try {
    const response = await fetch(`${backendUrl.replace(/\/$/, "")}/api/v1/auth/recovery`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: input.email.trim().toLowerCase(), redirect_to: redirectTo }),
      cache: "no-store", signal: AbortSignal.timeout(20_000),
    });
    if (response.ok) return accountResponse({ sent: true });
    const payload = await response.json().catch(() => ({})) as { detail?: unknown };
    const knownCodes = ["emailNotFound", "mailUnavailable", "recoveryUnavailable", "rateLimited"];
    return accountResponse({ code: typeof payload.detail === "string" && knownCodes.includes(payload.detail) ? payload.detail : "recoveryUnavailable" }, response.status);
  } catch {
    return accountResponse({ code: "recoveryUnavailable" }, 503);
  }
}
