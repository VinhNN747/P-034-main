import { describe, expect, it, vi } from "vitest";
import { createAuthConfirmation } from "./auth-confirmation";

function auth() {
  return {
    verifyOtp: vi.fn().mockResolvedValue({ data: { session: {} }, error: null }),
    setSession: vi.fn().mockResolvedValue({ data: { session: {} }, error: null }),
  };
}

describe("auth callback", () => {
  it("redeems a one-time recovery link once when an effect is replayed", async () => {
    const client = auth();
    const confirm = createAuthConfirmation(client, new URL("http://localhost:3000/auth/confirm?token_hash=valid-hash&type=recovery"));
    const first = confirm();
    const replay = confirm();
    expect(first).toBe(replay);
    expect(await first).toBe("/update-password");
    expect(await replay).toBe("/update-password");
    expect(client.verifyOtp).toHaveBeenCalledExactlyOnceWith({ token_hash: "valid-hash", type: "recovery" });
  });

  it("restores the session from a default recovery email without redeeming its token again", async () => {
    const client = auth();
    const confirm = createAuthConfirmation(client, new URL("http://localhost:3000/auth/confirm#access_token=access&refresh_token=refresh&type=recovery"));
    expect(await confirm()).toBe("/update-password");
    expect(client.setSession).toHaveBeenCalledWith({ access_token: "access", refresh_token: "refresh" });
    expect(client.verifyOtp).not.toHaveBeenCalled();
  });

  it.each([
    "?token_hash=hash&type=unsupported",
    "?type=recovery",
    "#error=access_denied&error_code=otp_expired",
    "#access_token=access&type=recovery",
  ])("rejects incomplete or failed callbacks (%s)", async (suffix) => {
    const client = auth();
    expect(await createAuthConfirmation(client, new URL(`http://localhost:3000/auth/confirm${suffix}`))()).toBe("/auth/error");
    expect(client.verifyOtp).not.toHaveBeenCalled();
    expect(client.setSession).not.toHaveBeenCalled();
  });

  it("does not treat a provider rejection as a verified recovery", async () => {
    const client = auth();
    client.verifyOtp.mockResolvedValue({ data: { session: null }, error: { code: "otp_expired" } });
    expect(await createAuthConfirmation(client, new URL("http://localhost:3000/auth/confirm?token_hash=expired&type=recovery"))()).toBe("/auth/error");
  });
});
