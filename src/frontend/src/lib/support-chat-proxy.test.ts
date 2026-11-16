import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ getClaims: vi.fn(), getSession: vi.fn() }));
vi.mock("@/lib/supabase/server", () => ({ createClient: async () => ({ auth }) }));

import { proxySupportChat } from "@/lib/support-chat-proxy";

function authorizedProfile() {
  return new Response(JSON.stringify({ roles: ["user"], permissions: ["applications.read_own"] }), { headers: { "Content-Type": "application/json" } });
}

describe("support proxy input boundaries", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    auth.getClaims.mockResolvedValue({ data: { claims: { sub: "user" } }, error: null });
    auth.getSession.mockResolvedValue({ data: { session: { access_token: "server-token" } } });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("rejects oversized streamed bodies before authentication or backend calls", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const response = await proxySupportChat(new Request("http://localhost/api/beta-chat", {
      method: "POST", body: JSON.stringify({ message: "x".repeat(25_000) }),
    }), "chat", "POST");
    expect(response.status).toBe(413);
    expect(auth.getClaims).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("accepts valid Vietnamese input within the backend character limit", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ message: "ok" }), {
      headers: { "Content-Type": "application/json" },
    })).mockResolvedValueOnce(authorizedProfile());
    vi.stubGlobal("fetch", fetch);
    const response = await proxySupportChat(new Request("http://localhost/api/beta-chat", {
      method: "POST", body: JSON.stringify({ message: "ệ".repeat(5000) }),
    }), "chat", "POST");
    expect(response.status).toBe(200);
    expect(fetch).toHaveBeenCalledWith(expect.any(URL), expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer server-token" }),
      signal: expect.any(AbortSignal),
    }));
  });

  it("rejects invalid JSON before contacting the backend", async () => {
    const response = await proxySupportChat(new Request("http://localhost/api/beta-chat", {
      method: "POST", body: "{broken",
    }), "chat", "POST");
    expect(response.status).toBe(400);
    expect(auth.getClaims).not.toHaveBeenCalled();
  });
});


describe("private chart data proxy", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("forwards authorized chart JSON and disables caching", async () => {
    auth.getClaims.mockResolvedValue({ data: { claims: { sub: "user" } }, error: null });
    auth.getSession.mockResolvedValue({ data: { session: { access_token: "server-token" } } });
    const data = { id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", rows: [{ label: "Today", value: 0.25 }] };
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } })).mockResolvedValueOnce(authorizedProfile());
    vi.stubGlobal("fetch", fetch);
    const response = await proxySupportChat(new Request("http://localhost/api/beta-chat/charts/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa?chart_type=bar"), "charts/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "GET");
    expect(response.headers.get("content-type")).toContain("application/json");
    expect(response.headers.get("cache-control")).toBe("private, no-store");
    expect(await response.json()).toEqual(data);
    const [url, options] = fetch.mock.calls[1];
    expect(url.search).toBe("");
    expect(options.headers.Authorization).toBe("Bearer server-token");
  });
});

describe("session deletion proxy", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    auth.getClaims.mockResolvedValue({ data: { claims: { sub: "user" } }, error: null });
    auth.getSession.mockResolvedValue({ data: { session: { access_token: "server-token" } } });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("forwards DELETE with authentication and preserves an empty 204 response", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 })).mockResolvedValueOnce(authorizedProfile());
    vi.stubGlobal("fetch", fetch);
    const response = await proxySupportChat(new Request("http://localhost/api/beta-chat/sessions/owned", { method: "DELETE" }), "sessions/owned", "DELETE");
    expect(response.status).toBe(204);
    expect(await response.text()).toBe("");
    expect(response.headers.get("cache-control")).toBe("no-store");
    const [url, options] = fetch.mock.calls[1];
    expect(url.pathname).toBe("/api/v1/support/sessions/owned");
    expect(options).toMatchObject({ method: "DELETE", headers: { Authorization: "Bearer server-token" } });
    expect(options.body).toBeUndefined();
  });

  it("preserves a permission failure so the UI can retain the session", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Insufficient permission" }), { status: 403, headers: { "Content-Type": "application/json" } })).mockResolvedValueOnce(authorizedProfile());
    vi.stubGlobal("fetch", fetch);
    const response = await proxySupportChat(new Request("http://localhost/api/beta-chat/sessions/foreign", { method: "DELETE" }), "sessions/foreign", "DELETE");
    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ detail: "Insufficient permission" });
  });
});

describe("completed authentication and account boundaries", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    auth.getClaims.mockResolvedValue({ data: { claims: { sub: "user-a", amr: [{ method: "password" }] } }, error: null });
    auth.getSession.mockResolvedValue({ data: { session: { access_token: "server-token" } } });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("rejects a recovery session before fetching authorization or chat data", async () => {
    auth.getClaims.mockResolvedValue({ data: { claims: { sub: "user-a", amr: [{ method: "recovery" }] } }, error: null });
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const response = await proxySupportChat(new Request("http://localhost/api/beta-chat/sessions"), "sessions", "GET");
    expect(response.status).toBe(403);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("rejects a pending request from a previously signed-in account", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const response = await proxySupportChat(new Request("http://localhost/api/beta-chat/sessions", { headers: { "X-Workspace-User": "user-b" } }), "sessions", "GET");
    expect(response.status).toBe(409);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("requires a valid signed-in session", async () => {
    auth.getSession.mockResolvedValue({ data: { session: null } });
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const response = await proxySupportChat(new Request("http://localhost/api/beta-chat/sessions"), "sessions", "GET");
    expect(response.status).toBe(401);
    expect(fetch).not.toHaveBeenCalled();
  });
});
