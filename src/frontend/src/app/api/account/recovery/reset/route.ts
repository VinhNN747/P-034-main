import { accountResponse, createVerificationClient, finishPasswordSession, passwordErrorCode, passwordInputError, readAccountInput } from "@/lib/account-security";
import { createClient } from "@/lib/supabase/server";

export async function POST(request: Request) {
  const input = await readAccountInput(request);
  if (!input || typeof input.email !== "string" || typeof input.token !== "string"
    || input.email.length > 254 || !/^\d{6,10}$/.test(input.token)) return accountResponse({ code: "invalidCode" }, 400);
  const invalidPassword = passwordInputError(input);
  if (invalidPassword) return accountResponse({ code: invalidPassword }, 400);
  const email = input.email.trim().toLowerCase();
  const verification = createVerificationClient();
  try {
    // An OTP is consumed before updating the password. A recovery session is
    // kept entirely on the server until the password has actually been saved.
    const verified = await verification.auth.verifyOtp({ email, token: input.token, type: "recovery" });
    if (verified.error || !verified.data.session || verified.data.user?.email?.toLowerCase() !== email) {
      return accountResponse({ code: "invalidCode" }, 403);
    }
    const password = input.password as string;
    const changed = await verification.auth.updateUser({ password });
    if (changed.error) return accountResponse({ code: passwordErrorCode(changed.error) }, 400);
    const client = await createClient();
    return await finishPasswordSession(client, email, password);
  } catch {
    return accountResponse({ code: "passwordUpdateFailed" }, 503);
  } finally {
    await verification.auth.signOut({ scope: "local" }).catch(() => undefined);
  }
}
