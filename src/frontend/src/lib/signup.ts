import type { SupabaseClient } from "@supabase/supabase-js";
import { AccountRequestError, postAccount } from "@/lib/account-client";

type SignupAuth = Pick<SupabaseClient["auth"], "verifyOtp" | "signOut">;
type SignupErrorKey = "account.error.rateLimited" | "account.error.invalidCode" | "account.error.passwordTooWeak"
  | "auth.signupEmailExists" | "auth.signupUnavailable" | "auth.signupConfirmationRequired"
  | "auth.signupAlreadyVerified" | "auth.signupNotFound" | "auth.signupVerifyFirst" | "account.error.mailUnavailable";

export class SignupError extends Error {
  constructor(readonly key: SignupErrorKey) { super(key); }
}

export function signupErrorKey(cause: unknown): SignupErrorKey {
  if (cause instanceof SignupError) return cause.key;
  if (cause instanceof AccountRequestError) {
    if (cause.code === "emailAlreadyVerified") return "auth.signupAlreadyVerified";
    if (cause.code === "signupNotFound") return "auth.signupNotFound";
    if (cause.code === "mailUnavailable") return "account.error.mailUnavailable";
    if (cause.code === "rateLimited") return "account.error.rateLimited";
    if (cause.code === "confirmationRequired") return "auth.signupConfirmationRequired";
    if (cause.code === "passwordTooWeak") return "account.error.passwordTooWeak";
    return "auth.signupUnavailable";
  }
  const error = cause as { code?: string; status?: number } | null;
  if (error?.status === 429 || error?.code?.startsWith("over_")) return "account.error.rateLimited";
  if (error?.code === "otp_expired" || error?.code === "otp_disabled") return "account.error.invalidCode";
  if (error?.code === "user_already_exists" || error?.code === "email_exists") return "auth.signupEmailExists";
  if (error?.code === "weak_password") return "account.error.passwordTooWeak";
  return "auth.signupUnavailable";
}

export async function requestSignup(auth: SignupAuth, email: string, password: string) {
  const normalizedEmail = email.trim().toLowerCase();
  // A previous account's session must not make a pending signup look active.
  const { error } = await auth.signOut({ scope: "local" });
  if (error) throw error;
  const result = await postAccount<{ pending: boolean }>("/api/account/signup/request", { email: normalizedEmail, password });
  if (result.pending !== true) throw new SignupError("auth.signupUnavailable");
  return normalizedEmail;
}

export async function resendSignup(email: string) {
  const result = await postAccount<{ requested: boolean }>("/api/account/signup/resend", { email: email.trim().toLowerCase() });
  if (result.requested !== true) throw new SignupError("auth.signupUnavailable");
}

export async function verifySignup(auth: SignupAuth, email: string, token: string) {
  const normalizedEmail = email.trim().toLowerCase();
  const normalizedToken = token.trim();
  if (!/^[0-9]{6,10}$/.test(normalizedToken)) throw new SignupError("account.error.invalidCode");
  const { data, error } = await auth.verifyOtp({ email: normalizedEmail, token: normalizedToken, type: "email" });
  if (error) throw error;
  if (!data.session || !data.user?.email_confirmed_at || data.user.email?.toLowerCase() !== normalizedEmail) {
    if (data.session) await auth.signOut({ scope: "local" });
    throw new SignupError("account.error.invalidCode");
  }
}

export async function signInVerified(auth: Pick<SupabaseClient["auth"], "signInWithPassword" | "signOut">, email: string, password: string) {
  const { data, error } = await auth.signInWithPassword({ email: email.trim().toLowerCase(), password });
  if (error?.code === "email_not_confirmed") throw new SignupError("auth.signupVerifyFirst");
  if (error) throw error;
  if (!data.user?.email_confirmed_at) {
    if (data.session) await auth.signOut({ scope: "local" });
    throw new SignupError("auth.signupVerifyFirst");
  }
  if (!data.session) throw new SignupError("auth.signupUnavailable");
}
