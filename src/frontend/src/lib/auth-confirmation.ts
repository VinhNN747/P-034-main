import type { EmailOtpType, SupabaseClient } from "@supabase/supabase-js";

type ConfirmationAuth = Pick<SupabaseClient["auth"], "verifyOtp" | "setSession">;
const emailTypes = new Set(["signup", "invite", "magiclink", "recovery", "email_change", "email"]);

async function confirm(auth: ConfirmationAuth, url: URL): Promise<string> {
  const query = url.searchParams;
  const hash = new URLSearchParams(url.hash.slice(1));
  if (query.has("error") || hash.has("error")) return "/auth/error";
  try {
    const tokenHash = query.get("token_hash");
    const type = query.get("type");
    if (tokenHash && type && emailTypes.has(type)) {
      const { data, error } = await auth.verifyOtp({ token_hash: tokenHash, type: type as EmailOtpType });
      if (error || !data.session) return "/auth/error";
      return type === "recovery" ? "/update-password" : "/dashboard";
    }
    const accessToken = hash.get("access_token");
    const refreshToken = hash.get("refresh_token");
    if (accessToken && refreshToken) {
      const { data, error } = await auth.setSession({ access_token: accessToken, refresh_token: refreshToken });
      if (error || !data.session) return "/auth/error";
      return hash.get("type") === "recovery" ? "/update-password" : "/dashboard";
    }
  } catch {
    // Never put tokens or raw provider errors into a URL or log.
  }
  return "/auth/error";
}

export function createAuthConfirmation(auth: ConfirmationAuth, url: URL) {
  let result: Promise<string> | undefined;
  // React Strict Mode replays effects in development. Reuse the operation so
  // the one-time recovery token is redeemed once, not once per effect setup.
  return () => result ??= confirm(auth, url);
}
