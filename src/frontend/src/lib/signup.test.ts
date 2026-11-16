import { afterEach, describe, expect, it, vi } from "vitest";
import { requestSignup, resendSignup, signInVerified, signupErrorKey, verifySignup } from "./signup";

afterEach(() => vi.unstubAllGlobals());

function auth() {
  return {
    signUp: vi.fn().mockResolvedValue({ data: { user: { identities: [{}] }, session: null }, error: null }),
    verifyOtp: vi.fn().mockResolvedValue({ data: { user: { email: "new@example.com", email_confirmed_at: "2026-10-18T00:00:00Z" }, session: {} }, error: null }),
    resend: vi.fn().mockResolvedValue({ error: null }),
    signOut: vi.fn().mockResolvedValue({ error: null }),
  };
}

describe("signup email verification", () => {
  it("requests a pending signup without redirecting or redeeming an OTP", async () => {
    const client = auth();
    const fetcher = vi.fn().mockResolvedValue(Response.json({ pending: true }));
    vi.stubGlobal("fetch", fetcher);
    expect(await requestSignup(client, " New@Example.com ", "strong-password")).toBe("new@example.com");
    expect(client.signOut).toHaveBeenCalledWith({ scope: "local" });
    expect(fetcher).toHaveBeenCalledWith("/api/account/signup/request", expect.objectContaining({
      body: JSON.stringify({ email: "new@example.com", password: "strong-password" }),
    }));
    expect(client.signUp).not.toHaveBeenCalled();
    expect(client.verifyOtp).not.toHaveBeenCalled();
  });

  it("does not accept auto-confirmed signups as completing OTP verification", async () => {
    const client = auth();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ code: "confirmationRequired" }, { status: 503 })));
    await expect(requestSignup(client, "new@example.com", "strong-password").catch((cause) => { throw signupErrorKey(cause); })).rejects.toBe("auth.signupConfirmationRequired");
    expect(client.signOut).toHaveBeenCalledWith({ scope: "local" });
  });

  it("handles obfuscated duplicate accounts without claiming an email was sent", async () => {
    const client = auth();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ code: "emailAlreadyVerified" }, { status: 409 })));
    await expect(requestSignup(client, "new@example.com", "strong-password").catch((cause) => { throw signupErrorKey(cause); })).rejects.toBe("auth.signupAlreadyVerified");
  });

  it("never proceeds with signup if a previous session cannot be cleared", async () => {
    const client = auth();
    client.signOut.mockResolvedValue({ error: new Error("signout failed") });
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    await expect(requestSignup(client, "new@example.com", "strong-password")).rejects.toThrow("signout failed");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not accept a successful HTTP response without a pending state", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({})));
    await expect(requestSignup(auth(), "new@example.com", "strong-password")).rejects.toMatchObject({ key: "auth.signupUnavailable" });
  });

  it("verifies the email and code before accepting a confirmed session", async () => {
    const client = auth();
    await verifySignup(client, " New@Example.com ", " 123456 ");
    expect(client.verifyOtp).toHaveBeenCalledWith({ email: "new@example.com", token: "123456", type: "email" });
  });

  it.each(["12345", "12345678901", "abc123", ""])('rejects malformed OTP "%s" before sending it', async (token) => {
    const client = auth();
    await expect(verifySignup(client, "new@example.com", token)).rejects.toMatchObject({ key: "account.error.invalidCode" });
    expect(client.verifyOtp).not.toHaveBeenCalled();
  });

  it("keeps invalid, expired or reused OTPs unverified", async () => {
    const client = auth();
    client.verifyOtp.mockResolvedValue({ data: { user: null, session: null }, error: { code: "otp_expired" } });
    await expect(verifySignup(client, "new@example.com", "123456")).rejects.toMatchObject({ code: "otp_expired" });
    expect(signupErrorKey({ code: "otp_expired" })).toBe("account.error.invalidCode");
  });

  it.each([
    { user: { email: "new@example.com", email_confirmed_at: "2026-10-18" }, session: null },
    { user: { email: "new@example.com" }, session: {} },
    { user: { email: "other@example.com", email_confirmed_at: "2026-10-18" }, session: {} },
  ])("rejects a missing session, unconfirmed email or different account", async (data) => {
    const client = auth();
    client.verifyOtp.mockResolvedValue({ data, error: null });
    await expect(verifySignup(client, "new@example.com", "123456")).rejects.toMatchObject({ key: "account.error.invalidCode" });
    if (data.session) expect(client.signOut).toHaveBeenCalledWith({ scope: "local" });
  });

  it("checks pending signup state through the server before resending", async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({ requested: true }));
    vi.stubGlobal("fetch", fetcher);
    await resendSignup(" New@Example.com ");
    expect(fetcher).toHaveBeenCalledWith("/api/account/signup/resend", expect.objectContaining({
      method: "POST", body: JSON.stringify({ email: "new@example.com" }), cache: "no-store",
    }));
  });

  it.each([
    ["emailAlreadyVerified", "auth.signupAlreadyVerified"],
    ["signupNotFound", "auth.signupNotFound"],
    ["rateLimited", "account.error.rateLimited"],
    ["mailUnavailable", "account.error.mailUnavailable"],
  ])("does not announce mail sent when the server reports %s", async (code, key) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ code }, { status: 409 })));
    await expect(resendSignup("new@example.com").catch((cause) => { throw signupErrorKey(cause); })).rejects.toBe(key);
  });

  it("rejects an unacknowledged resend response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({})));
    await expect(resendSignup("new@example.com")).rejects.toMatchObject({ key: "auth.signupUnavailable" });
  });
});

describe("email verification required at login", () => {
  it.each([
    { data: { user: null, session: null }, error: { code: "email_not_confirmed" } },
    { data: { user: { email_confirmed_at: null }, session: {} }, error: null },
  ])("rejects pending accounts even if a provider returns a session", async (result) => {
    const client = { signInWithPassword: vi.fn().mockResolvedValue(result), signOut: vi.fn().mockResolvedValue({ error: null }) };
    await expect(signInVerified(client, "new@example.com", "password123")).rejects.toMatchObject({ key: "auth.signupVerifyFirst" });
    if (result.data.session) expect(client.signOut).toHaveBeenCalledWith({ scope: "local" });
  });

  it("allows login only with a confirmed email and session", async () => {
    const client = { signInWithPassword: vi.fn().mockResolvedValue({ data: { user: { email_confirmed_at: "2026-10-18" }, session: {} }, error: null }), signOut: vi.fn() };
    await signInVerified(client, " New@Example.com ", "password123");
    expect(client.signInWithPassword).toHaveBeenCalledWith({ email: "new@example.com", password: "password123" });
    expect(client.signOut).not.toHaveBeenCalled();
  });
});
