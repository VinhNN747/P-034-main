import { beforeEach, describe, expect, it, vi } from "vitest";

const { sessionAuth, verificationAuth } = vi.hoisted(() => ({
  sessionAuth: { getUser: vi.fn(), getClaims: vi.fn(), updateUser: vi.fn(), signInWithPassword: vi.fn(), signOut: vi.fn() },
  verificationAuth: { signInWithPassword: vi.fn(), updateUser: vi.fn(), verifyOtp: vi.fn(), signOut: vi.fn() },
}));
vi.mock("@/lib/supabase/server", () => ({ createClient: async () => ({ auth: sessionAuth }) }));
vi.mock("@supabase/supabase-js", () => ({ createClient: () => ({ auth: verificationAuth }) }));

import { GET, POST } from "@/app/api/account/password/route";
import { POST as requestRecovery } from "@/app/api/account/recovery/request/route";
import { POST as recoverPassword } from "@/app/api/account/recovery/reset/route";
import { hasRecentRecoveryConfirmation } from "@/lib/auth-session";

const user = { id: "user-a", email: "user-a@example.com" };
const newPassword = "a-new-password-123";
const validPassword = { password: newPassword, confirmPassword: newPassword, currentPassword: "old-password" };

function request(input: Record<string, unknown>, origin = "http://localhost") {
  return new Request("http://localhost/api/account/password", {
    method: "POST", headers: { "Content-Type": "application/json", Origin: origin }, body: JSON.stringify(input),
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  sessionAuth.getUser.mockResolvedValue({ data: { user }, error: null });
  sessionAuth.getClaims.mockResolvedValue({ data: { claims: { sub: user.id, amr: [{ method: "password" }] } }, error: null });
  sessionAuth.updateUser.mockResolvedValue({ data: { user }, error: null });
  sessionAuth.signInWithPassword.mockResolvedValue({ data: { user, session: {} }, error: null });
  sessionAuth.signOut.mockResolvedValue({ error: null });
  verificationAuth.signInWithPassword.mockResolvedValue({ data: { user, session: {} }, error: null });
  verificationAuth.verifyOtp.mockResolvedValue({ data: { user, session: {} }, error: null });
  verificationAuth.updateUser.mockResolvedValue({ data: { user }, error: null });
  verificationAuth.signOut.mockResolvedValue({ error: null });
});

describe("password confirmation enforced by the server", () => {
  it("does not accept a client recovery flag as proof of identity", async () => {
    const response = await POST(request({ password: newPassword, confirmPassword: newPassword, mode: "recovery" }));
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ code: "currentPasswordRequired" });
    expect(verificationAuth.updateUser).not.toHaveBeenCalled();
    expect(sessionAuth.updateUser).not.toHaveBeenCalled();
  });

  it("rejects an incorrect old password without changing the account", async () => {
    verificationAuth.signInWithPassword.mockResolvedValue({ data: { user: null }, error: { code: "invalid_credentials" } });
    const response = await POST(request(validPassword));
    expect(response.status).toBe(403);
    expect(verificationAuth.updateUser).not.toHaveBeenCalled();
    expect(sessionAuth.signInWithPassword).not.toHaveBeenCalled();
  });

  it("uses the session's email and checks the verified user ID", async () => {
    verificationAuth.signInWithPassword.mockResolvedValue({ data: { user: { id: "user-b" } }, error: null });
    const response = await POST(request({ ...validPassword, email: "user-b@example.com" }));
    expect(response.status).toBe(403);
    expect(verificationAuth.signInWithPassword).toHaveBeenCalledWith({ email: user.email, password: "old-password" });
    expect(verificationAuth.updateUser).not.toHaveBeenCalled();
  });

  it("rejects a form belonging to an account that has since been switched", async () => {
    const response = await POST(request({ ...validPassword, accountId: "previous-user" }));
    expect(response.status).toBe(409);
    expect(verificationAuth.signInWithPassword).not.toHaveBeenCalled();
    expect(verificationAuth.updateUser).not.toHaveBeenCalled();
  });

  it("updates only after old-password verification and issues a fresh dashboard session", async () => {
    const response = await POST(request(validPassword));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ saved: true, next: "/dashboard" });
    expect(verificationAuth.updateUser).toHaveBeenCalledWith({ password: newPassword, current_password: "old-password" });
    expect(sessionAuth.signInWithPassword).toHaveBeenCalledWith({ email: user.email, password: newPassword });
    expect(response.headers.get("cache-control")).toBe("private, no-store");
  });

  it("rejects a cross-origin password change before authenticating", async () => {
    const response = await POST(request(validPassword, "https://unrelated.example"));
    expect(response.status).toBe(400);
    expect(sessionAuth.getUser).not.toHaveBeenCalled();
  });

  it("rejects mismatched new passwords before contacting Supabase", async () => {
    const response = await POST(request({ ...validPassword, confirmPassword: "different-password" }));
    expect(response.status).toBe(400);
    expect(verificationAuth.signInWithPassword).not.toHaveBeenCalled();
  });

  it("rejects a signed recovery confirmation after its time window", async () => {
    sessionAuth.getClaims.mockResolvedValue({ data: { claims: { sub: user.id, amr: [{ method: "recovery", timestamp: Date.now() / 1000 - 3600 }] } }, error: null });
    const response = await POST(request({ password: newPassword, confirmPassword: newPassword }));
    expect(response.status).toBe(403);
    expect(sessionAuth.updateUser).not.toHaveBeenCalled();
  });

  it("allows a recent verified recovery link, then replaces its recovery session", async () => {
    sessionAuth.getClaims.mockResolvedValue({ data: { claims: { sub: user.id, amr: [{ method: "recovery", timestamp: Date.now() / 1000 }] } }, error: null });
    expect((await GET()).status).toBe(200);
    const response = await POST(request({ password: newPassword, confirmPassword: newPassword }));
    expect(await response.json()).toEqual({ saved: true, next: "/dashboard" });
    expect(sessionAuth.updateUser).toHaveBeenCalledWith({ password: newPassword });
    expect(sessionAuth.signOut).toHaveBeenCalledWith({ scope: "local" });
  });
});

describe("email recovery confirmation", () => {
  const input = { email: user.email, token: "123456", password: newPassword, confirmPassword: newPassword };
  it("does not change a password or issue cookies for an invalid or expired code", async () => {
    verificationAuth.verifyOtp.mockResolvedValue({ data: { user: null, session: null }, error: { code: "otp_expired" } });
    const response = await recoverPassword(request(input));
    expect(response.status).toBe(403);
    expect(verificationAuth.updateUser).not.toHaveBeenCalled();
    expect(sessionAuth.signInWithPassword).not.toHaveBeenCalled();
  });

  it("binds the recovery code to the requested email", async () => {
    verificationAuth.verifyOtp.mockResolvedValue({ data: { user: { email: "other@example.com" }, session: {} }, error: null });
    const response = await recoverPassword(request(input));
    expect(response.status).toBe(403);
    expect(verificationAuth.updateUser).not.toHaveBeenCalled();
  });

  it("saves after verifying an email recovery OTP, then creates a normal login", async () => {
    const response = await recoverPassword(request(input));
    expect(response.status).toBe(200);
    expect(verificationAuth.verifyOtp).toHaveBeenCalledWith({ email: user.email, token: "123456", type: "recovery" });
    expect(verificationAuth.updateUser).toHaveBeenCalledWith({ password: newPassword });
    expect(sessionAuth.signInWithPassword).toHaveBeenCalledWith({ email: user.email, password: newPassword });
    expect(verificationAuth.signOut).toHaveBeenCalledWith({ scope: "local" });
  });

  it("rejects future or absent recovery timestamps", () => {
    expect(hasRecentRecoveryConfirmation({ amr: [{ method: "recovery" }] })).toBe(false);
    expect(hasRecentRecoveryConfirmation({ amr: [{ method: "recovery", timestamp: Date.now() / 1000 + 3600 }] })).toBe(false);
  });

  it("reports a saved password accurately if the final sign-in is temporarily unavailable", async () => {
    sessionAuth.signInWithPassword.mockRejectedValue(new Error("Network unavailable"));
    const response = await recoverPassword(request(input));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ saved: true, next: "/login" });
    expect(verificationAuth.updateUser).toHaveBeenCalledOnce();
  });
});

describe("email recovery request", () => {
  it("sends the current frontend callback to the recovery service", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ sent: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    try {
      const response = await requestRecovery(new Request("http://localhost:3000/api/account/recovery/request", {
        method: "POST",
        headers: { "Content-Type": "application/json", Origin: "http://localhost:3000" },
        body: JSON.stringify({ email: "  User@Example.com " }),
      }));
      expect(response.status).toBe(200);
      const [, options] = fetchMock.mock.calls[0];
      expect(JSON.parse(String(options?.body))).toEqual({
        email: "user@example.com",
        redirect_to: "http://localhost:3000/auth/confirm",
      });
    } finally {
      fetchMock.mockRestore();
    }
  });
});
