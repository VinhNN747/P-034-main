import { accountResponse, createVerificationClient, finishPasswordSession, passwordErrorCode, passwordInputError, readAccountInput } from "@/lib/account-security";
import { hasRecentRecoveryConfirmation, isRecoverySession } from "@/lib/auth-session";
import { createClient } from "@/lib/supabase/server";

export async function GET() {
  try {
    const client = await createClient();
    const { data, error } = await client.auth.getUser();
    if (error || !data.user?.email) return accountResponse({ code: "authenticationRequired" }, 401);
    const claims = await client.auth.getClaims();
    if (claims.error || claims.data?.claims.sub !== data.user.id) return accountResponse({ code: "authenticationRequired" }, 401);
    if (isRecoverySession(claims.data.claims) && !hasRecentRecoveryConfirmation(claims.data.claims)) return accountResponse({ code: "invalidCode" }, 403);
    return accountResponse({ id: data.user.id, email: data.user.email, mode: isRecoverySession(claims.data.claims) ? "recovery" : "change" });
  } catch {
    return accountResponse({ code: "recoveryUnavailable" }, 503);
  }
}

export async function POST(request: Request) {
  const input = await readAccountInput(request);
  if (!input) return accountResponse({ code: "invalidRequest" }, 400);
  const invalidPassword = passwordInputError(input);
  if (invalidPassword) return accountResponse({ code: invalidPassword }, 400);

  try {
    const client = await createClient();
    const { data, error } = await client.auth.getUser();
    if (error || !data.user?.email) return accountResponse({ code: "authenticationRequired" }, 401);
    if (input.accountId !== undefined && input.accountId !== data.user.id) return accountResponse({ code: "accountChanged" }, 409);
    const claims = await client.auth.getClaims();
    if (claims.error || claims.data?.claims.sub !== data.user.id) return accountResponse({ code: "authenticationRequired" }, 401);
    const password = input.password as string;

    if (isRecoverySession(claims.data.claims)) {
      if (!hasRecentRecoveryConfirmation(claims.data.claims)) return accountResponse({ code: "invalidCode" }, 403);
      const changed = await client.auth.updateUser({ password });
      if (changed.error) return accountResponse({ code: passwordErrorCode(changed.error) }, 400);
      await client.auth.signOut({ scope: "local" }).catch(() => undefined);
    } else {
      if (typeof input.currentPassword !== "string" || !input.currentPassword || input.currentPassword.length > 128) {
        return accountResponse({ code: "currentPasswordRequired" }, 400);
      }
      if (input.currentPassword === password) return accountResponse({ code: "passwordUnchanged" }, 400);
      const verification = createVerificationClient();
      try {
        // Verify against the identity from the authenticated session, never an
        // email supplied by the browser. This does not replace its cookies.
        const verified = await verification.auth.signInWithPassword({ email: data.user.email, password: input.currentPassword });
        if (verified.error) {
          const code = passwordErrorCode(verified.error, verified.error.code === "invalid_credentials" ? "currentPasswordIncorrect" : "recoveryUnavailable");
          return accountResponse({ code }, code === "currentPasswordIncorrect" ? 403 : code === "rateLimited" ? 429 : 503);
        }
        if (verified.data.user?.id !== data.user.id) {
          return accountResponse({ code: "currentPasswordIncorrect" }, 403);
        }
        const changed = await verification.auth.updateUser({ password, current_password: input.currentPassword });
        if (changed.error) return accountResponse({ code: passwordErrorCode(changed.error) }, 400);
      } finally {
        await verification.auth.signOut({ scope: "local" }).catch(() => undefined);
      }
    }

    // Issue a normal password session after a successful change, including
    // recovery flows. The browser returns to the dashboard with fresh cookies.
    return await finishPasswordSession(client, data.user.email, password);
  } catch {
    return accountResponse({ code: "passwordUpdateFailed" }, 503);
  }
}
