import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import { NextResponse } from "next/server";

export function createVerificationClient() {
  return createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
    { auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false, flowType: "implicit" } },
  );
}

export function accountResponse(body: Record<string, unknown>, status = 200) {
  return NextResponse.json(body, { status, headers: { "Cache-Control": "private, no-store" } });
}

export async function finishPasswordSession(client: SupabaseClient, email: string, password: string) {
  // The password has already been saved. A sign-in outage must not be reported
  // as a failed password update and encourage repeating a consumed OTP.
  try {
    const signedIn = await client.auth.signInWithPassword({ email, password });
    return accountResponse({ saved: true, next: signedIn.error ? "/login" : "/dashboard" });
  } catch {
    return accountResponse({ saved: true, next: "/login" });
  }
}

export async function readAccountInput(request: Request): Promise<Record<string, unknown> | null> {
  const origin = request.headers.get("origin");
  const forwardedProto = request.headers.get("x-forwarded-proto")?.split(",")[0]?.trim();
  const forwardedHost = (request.headers.get("x-forwarded-host") || request.headers.get("host"))?.split(",")[0]?.trim();
  const requestOrigin = forwardedHost
    ? `${forwardedProto || new URL(request.url).protocol.replace(":", "")}://${forwardedHost}`
    : new URL(request.url).origin;
  if ((origin && origin !== requestOrigin)
    || request.headers.get("sec-fetch-site") === "cross-site"
    || !request.headers.get("content-type")?.startsWith("application/json")) return null;
  const reader = request.body?.getReader();
  if (!reader) return null;
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > 8_192) {
        await reader.cancel();
        return null;
      }
      chunks.push(value);
    }
    const bytes = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    const input: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    return input && typeof input === "object" && !Array.isArray(input) ? input as Record<string, unknown> : null;
  } catch {
    return null;
  } finally {
    reader.releaseLock();
  }
}

export function passwordInputError(input: Record<string, unknown>) {
  if (typeof input.password !== "string" || input.password.length < 8 || input.password.length > 128) return "passwordTooWeak";
  if (input.password !== input.confirmPassword) return "passwordMismatch";
  return null;
}

export function passwordErrorCode(error: { code?: string }, fallback = "passwordUpdateFailed") {
  if (error.code === "same_password") return "passwordUnchanged";
  if (error.code === "weak_password") return "passwordTooWeak";
  if (error.code === "over_request_rate_limit" || error.code === "over_email_send_rate_limit") return "rateLimited";
  return fallback;
}
