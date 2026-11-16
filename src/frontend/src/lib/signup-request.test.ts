import { afterEach, describe, expect, it, vi } from "vitest";
import { POST } from "@/app/api/account/signup/request/route";

afterEach(() => vi.unstubAllGlobals());

function request(password = "password123", origin = "https://taskway.example.com") {
  return new Request("https://taskway.example.com/api/account/signup/request", {
    method: "POST", headers: { "Content-Type": "application/json", Origin: origin },
    body: JSON.stringify({ email: " New@Example.com ", password }),
  });
}

describe("signup uses the same request-then-verify pattern as password recovery", () => {
  it("returns pending instead of registration success or session credentials", async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({ pending: true, access_token: "must-not-forward" }));
    vi.stubGlobal("fetch", fetcher);
    const response = await POST(request());
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ pending: true });
    expect(response.headers.get("set-cookie")).toBeNull();
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining("/api/v1/auth/signup/request"), expect.objectContaining({
      body: JSON.stringify({ email: "new@example.com", password: "password123" }), cache: "no-store",
    }));
  });

  it("does not turn an unacknowledged backend response into a successful signup", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ session: {} })));
    expect((await POST(request())).status).toBe(503);
  });

  it.each(["short", "x".repeat(129)])("validates password length before requesting signup", async (password) => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect((await POST(request(password))).status).toBe(400);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("rejects a cross-origin request before sending credentials", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect((await POST(request("password123", "https://other.example.com"))).status).toBe(400);
    expect(fetcher).not.toHaveBeenCalled();
  });
});
