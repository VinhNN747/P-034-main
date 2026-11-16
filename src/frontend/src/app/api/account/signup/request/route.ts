import { accountResponse, readAccountInput } from "@/lib/account-security";

export async function POST(request: Request) {
  const input = await readAccountInput(request);
  if (!input || typeof input.email !== "string" || input.email.length > 254) return accountResponse({ code: "invalidRequest" }, 400);
  if (typeof input.password !== "string" || input.password.length < 8 || input.password.length > 128) return accountResponse({ code: "passwordTooWeak" }, 400);
  const backendUrl = process.env.BACKEND_API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
  try {
    const response = await fetch(`${backendUrl.replace(/\/$/, "")}/api/v1/auth/signup/request`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: input.email.trim().toLowerCase(), password: input.password }),
      cache: "no-store", signal: AbortSignal.timeout(20_000),
    });
    const payload = await response.json().catch(() => ({})) as { detail?: unknown; pending?: unknown };
    if (response.ok) return payload.pending === true
      ? accountResponse({ pending: true })
      : accountResponse({ code: "signupUnavailable" }, 503);
    const codes = ["emailAlreadyVerified", "mailUnavailable", "signupUnavailable", "rateLimited", "confirmationRequired", "passwordTooWeak"];
    return accountResponse({ code: typeof payload.detail === "string" && codes.includes(payload.detail) ? payload.detail : "signupUnavailable" }, response.status);
  } catch {
    return accountResponse({ code: "signupUnavailable" }, 503);
  }
}
