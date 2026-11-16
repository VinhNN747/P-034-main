import { afterEach, describe, expect, it, vi } from "vitest";
import { POST } from "@/app/api/account/signup/resend/route";

afterEach(() => vi.unstubAllGlobals());

function request(email: string, origin = "https://taskway.example.com") {
  return new Request("https://taskway.example.com/api/account/signup/resend", {
    method: "POST", headers: { "Content-Type": "application/json", Origin: origin }, body: JSON.stringify({ email }),
  });
}

describe("signup resend proxy", () => {
  it("forwards only the normalized email and acknowledges the request", async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({ requested: true }));
    vi.stubGlobal("fetch", fetcher);
    const response = await POST(request(" New@Example.com "));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ requested: true });
    expect(response.headers.get("cache-control")).toBe("private, no-store");
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining("/api/v1/auth/signup/resend"), expect.objectContaining({
      method: "POST", body: JSON.stringify({ email: "new@example.com" }), cache: "no-store",
    }));
  });

  it.each([[409, "emailAlreadyVerified"], [404, "signupNotFound"], [429, "rateLimited"], [503, "mailUnavailable"]])(
    "preserves the server's %s response instead of claiming success", async (status, detail) => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ detail }, { status })));
      const response = await POST(request("new@example.com"));
      expect(response.status).toBe(status);
      expect(await response.json()).toEqual({ code: detail });
    },
  );

  it("rejects cross-origin requests before contacting the backend", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect((await POST(request("new@example.com", "https://other.example.com"))).status).toBe(400);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("reports an unavailable backend as a failed resend", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("private connection details")));
    const response = await POST(request("new@example.com"));
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ code: "signupUnavailable" });
  });
});
