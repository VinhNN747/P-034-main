import { NextResponse } from "next/server";

import { createClient } from "@/lib/supabase/server";
import { isAuthenticationComplete } from "@/lib/auth-session";

const MAX_REQUEST_BYTES = 24_000;

async function boundedBody(request: Request): Promise<string | null> {
  if (Number(request.headers.get("content-length")) > MAX_REQUEST_BYTES) return null;
  if (!request.body) return "";
  const reader = request.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let size = 0;
  let text = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_REQUEST_BYTES) {
        await reader.cancel();
        return null;
      }
      text += decoder.decode(value, { stream: true });
    }
    return text + decoder.decode();
  } finally {
    reader.releaseLock();
  }
}

export async function proxySupportChat(request: Request, path: string, method: "GET" | "POST" | "PATCH" | "DELETE") {
  const isChart = /^charts\/[0-9a-f-]{36}$/i.test(path) && method === "GET";
  let body: string | undefined;
  if (method === "POST" || method === "PATCH") {
    try {
      const parsedBody = await boundedBody(request);
      if (parsedBody === null) {
        return NextResponse.json({ error: "The message is too large." }, { status: 413 });
      }
      body = parsedBody;
    } catch {
      return NextResponse.json({ error: "The message is invalid." }, { status: 400 });
    }
    if (new TextEncoder().encode(body).byteLength > MAX_REQUEST_BYTES) {
      return NextResponse.json({ error: "The message is too large." }, { status: 413 });
    }
    if (body) {
      try {
        JSON.parse(body);
      } catch {
        return NextResponse.json({ error: "The message is invalid." }, { status: 400 });
      }
    }
  }

  const supabase = await createClient();
  const { data: sessionData } = await supabase.auth.getSession();
  const { data: claims, error: authError } = await supabase.auth.getClaims(sessionData.session?.access_token);
  if (authError || !claims?.claims || !sessionData.session) {
    return NextResponse.json({ error: "You must sign in to use the assistant." }, { status: 401 });
  }
  if (!isAuthenticationComplete(claims.claims)) {
    return NextResponse.json({ error: "Complete sign-in before using the assistant." }, { status: 403, headers: { "Cache-Control": "no-store" } });
  }
  const expectedUser = request.headers.get("X-Workspace-User");
  if (expectedUser && expectedUser !== claims.claims.sub) {
    return NextResponse.json({ error: "Your account changed. Reopen the assistant." }, { status: 409, headers: { "Cache-Control": "no-store" } });
  }

  const backendUrl = process.env.BACKEND_API_URL
    ?? process.env.NEXT_PUBLIC_API_URL
    ?? "http://127.0.0.1:8000";
  let authorizationResponse: Response;
  try {
    authorizationResponse = await fetch(`${backendUrl.replace(/\/$/, "")}/api/v1/authorization/me`, {
      headers: { Authorization: `Bearer ${sessionData.session.access_token}` },
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });
  } catch {
    return NextResponse.json({ error: "Your access could not be verified." }, { status: 503 });
  }
  if (!authorizationResponse.ok) {
    const status = authorizationResponse.status === 401 ? 401 : authorizationResponse.status === 403 ? 403 : 503;
    return NextResponse.json(
      { error: status === 401
        ? "You must sign in to use the assistant."
        : status === 403
          ? "You are not authorized to use the assistant."
          : "Your access could not be verified." },
      { status },
    );
  }

  let authorizationProfile: { roles?: unknown; permissions?: unknown };
  try {
    authorizationProfile = await authorizationResponse.json() as { roles?: unknown; permissions?: unknown };
  } catch {
    return NextResponse.json({ error: "Your access could not be verified." }, { status: 503 });
  }
  const roles = Array.isArray(authorizationProfile.roles) ? authorizationProfile.roles : [];
  const permissions = Array.isArray(authorizationProfile.permissions) ? authorizationProfile.permissions : [];
  const canUseAssistant = roles.includes("user")
    && !roles.includes("admin")
    && !roles.includes("platform_admin")
    && permissions.includes("applications.read_own");
  if (!canUseAssistant) {
    return NextResponse.json({ error: "You are not authorized to use the assistant." }, { status: 403 });
  }

  const target = new URL(`/api/v1/support/${path}`, backendUrl);
  if (method === "GET") {
    const params = new URL(request.url).searchParams;
    for (const key of (isChart ? [] : ["offset", "limit"])) {
      const value = params.get(key);
      if (value !== null) target.searchParams.set(key, value);
    }
  }

  try {
    const response = await fetch(target, {
      method,
      headers: {
        Authorization: `Bearer ${sessionData.session.access_token}`,
        ...(method === "POST" || method === "PATCH" ? { "Content-Type": "application/json" } : {}),
      },
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(120_000),
    });
    if (method === "DELETE" && response.status === 204) {
      return new NextResponse(null, { status: 204, headers: { "Cache-Control": "no-store" } });
    }
    const contentType = response.headers.get("content-type") ?? "";
    const payload: unknown = contentType.includes("application/json")
      ? await response.json()
      : {
        error: response.ok
          ? "The assistant service returned an invalid response."
          : `The assistant service is temporarily unavailable (status ${response.status}).`,
      };
    if (!contentType.includes("application/json")) {
      console.error("Support chatbot backend returned a non-JSON response", response.status);
    }
    return NextResponse.json(payload, {
      status: response.status,
      headers: { "Cache-Control": isChart ? "private, no-store" : "no-store" },
    });
  } catch (error) {
    console.error("Support chatbot proxy failed", error instanceof Error ? error.name : "Unknown error");
    return NextResponse.json(
      { error: "Unable to connect to the assistant right now. Please try again." },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}
