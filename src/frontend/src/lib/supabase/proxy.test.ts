import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const { auth } = vi.hoisted(() => ({ auth: { getClaims: vi.fn(), getUser: vi.fn() } }));
vi.mock("@supabase/ssr", () => ({ createServerClient: () => ({ auth }) }));
import { updateSession } from "./proxy";

beforeEach(() => {
  vi.resetAllMocks();
  auth.getClaims.mockResolvedValue({ data: { claims: { sub: "user-a", amr: [{ method: "password" }] } } });
  auth.getUser.mockResolvedValue({ data: { user: { id: "user-a", email_confirmed_at: "2026-10-18" } }, error: null });
});

describe("protected pages require a confirmed email", () => {
  it("sends an unconfirmed session to OTP entry", async () => {
    auth.getUser.mockResolvedValue({ data: { user: { id: "user-a", email_confirmed_at: null } }, error: null });
    const response = await updateSession(new NextRequest("https://taskway.example.com/dashboard"));
    expect(response.headers.get("location")).toBe("https://taskway.example.com/sign-up?verify=1");
  });

  it("does not trust editable email_verified metadata", async () => {
    auth.getUser.mockResolvedValue({ data: { user: { id: "user-a", email_confirmed_at: null, user_metadata: { email_verified: true } } }, error: null });
    const response = await updateSession(new NextRequest("https://taskway.example.com/chat"));
    expect(response.headers.get("location")).toBe("https://taskway.example.com/sign-up?verify=1");
  });

  it("allows a confirmed account", async () => {
    const response = await updateSession(new NextRequest("https://taskway.example.com/dashboard"));
    expect(response.headers.get("location")).toBeNull();
  });

  it("blocks a stale session when Auth no longer accepts it", async () => {
    auth.getUser.mockResolvedValue({ data: { user: null }, error: { code: "session_not_found" } });
    const response = await updateSession(new NextRequest("https://taskway.example.com/dashboard"));
    expect(response.headers.get("location")).toBe("https://taskway.example.com/login");
  });

  it("keeps a confirmed recovery session in the password reset flow", async () => {
    auth.getClaims.mockResolvedValue({ data: { claims: { sub: "user-a", amr: [{ method: "recovery" }] } } });
    const response = await updateSession(new NextRequest("https://taskway.example.com/dashboard"));
    expect(response.headers.get("location")).toBe("https://taskway.example.com/update-password");
  });

  it("does not block public OTP entry", async () => {
    await updateSession(new NextRequest("https://taskway.example.com/sign-up?verify=1"));
    expect(auth.getUser).not.toHaveBeenCalled();
  });
});
