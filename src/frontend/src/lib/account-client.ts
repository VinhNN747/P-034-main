const errorKeys = {
  accountChanged: "account.error.accountChanged",
  currentPasswordRequired: "account.error.currentPasswordRequired",
  currentPasswordIncorrect: "account.error.currentPasswordIncorrect",
  emailNotFound: "account.error.emailNotFound",
  invalidCode: "account.error.invalidCode",
  passwordTooWeak: "account.error.passwordTooWeak",
  passwordMismatch: "account.error.passwordMismatch",
  passwordUnchanged: "account.error.passwordUnchanged",
  authenticationRequired: "account.error.authenticationRequired",
  recoveryUnavailable: "account.error.recoveryUnavailable",
  mailUnavailable: "account.error.mailUnavailable",
  rateLimited: "account.error.rateLimited",
  passwordUpdateFailed: "account.error.passwordUpdateFailed",
  invalidRequest: "account.error.invalidRequest",
} as const;

export class AccountRequestError extends Error {
  constructor(readonly code: string) { super(code); }
}

export function accountErrorKey(code: string) {
  return errorKeys[code as keyof typeof errorKeys] ?? errorKeys.passwordUpdateFailed;
}

export async function readAccountResponse<T>(response: Response): Promise<T> {
  const payload = await response.json() as T & { code?: string };
  if (!response.ok) throw new AccountRequestError(payload.code ?? "recoveryUnavailable");
  return payload;
}

export async function postAccount<T>(path: string, body: Record<string, unknown>) {
  return readAccountResponse<T>(await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body), cache: "no-store",
  }));
}

const recoveryEmailKey = "taskway:recovery-email";

export function readRecoveryEmail() {
  try { return window.sessionStorage.getItem(recoveryEmailKey) ?? ""; } catch { return ""; }
}

export function rememberRecoveryEmail(email: string) {
  try { window.sessionStorage.setItem(recoveryEmailKey, email); } catch { /* Storage is optional. */ }
}

export function clearRecoveryEmail() {
  try { window.sessionStorage.removeItem(recoveryEmailKey); } catch { /* Storage is optional. */ }
}

export function finishPasswordChange(next: string | undefined) {
  clearRecoveryEmail();
  // Reload the new session and clear in-memory data of the previous account.
  window.location.replace(next === "/login" ? "/login" : "/dashboard");
}
