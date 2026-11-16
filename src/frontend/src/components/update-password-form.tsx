"use client";

import Link from "next/link";
import { type FormEvent, useEffect, useState } from "react";

import { useLanguage } from "@/components/language-provider";
import { AccountRequestError, accountErrorKey, finishPasswordChange, postAccount, readAccountResponse } from "@/lib/account-client";

type Account = { id: string; email: string; mode: "change" | "recovery" };
type PasswordField = "current" | "new" | "confirm";

export function UpdatePasswordForm() {
  const [account, setAccount] = useState<Account | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [visibleField, setVisibleField] = useState<PasswordField | null>(null);
  const { t } = useLanguage();

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/account/password", { cache: "no-store", signal: controller.signal })
      .then((response) => readAccountResponse<Account>(response))
      .then((profile) => { if (!controller.signal.aborted) setAccount(profile); })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(cause instanceof AccountRequestError ? cause.code : "recoveryUnavailable");
      });
    return () => controller.abort();
  }, []);

  function togglePassword(field: PasswordField) {
    setVisibleField((current) => current === field ? null : field);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving || !account) return;
    const form = new FormData(event.currentTarget);
    const password = String(form.get("password") ?? "");
    const confirmPassword = String(form.get("confirmPassword") ?? "");
    if (password !== confirmPassword) { setError("passwordMismatch"); return; }
    setError("");
    setSaving(true);
    try {
      const result = await postAccount<{ saved: boolean; next?: string }>("/api/account/password", {
        accountId: account.id,
        password,
        confirmPassword,
        currentPassword: String(form.get("currentPassword") ?? ""),
      });
      finishPasswordChange(result.next);
    } catch (cause) {
      setError(cause instanceof AccountRequestError ? cause.code : "passwordUpdateFailed");
      setSaving(false);
    }
  }

  if (!account) return (
    <div className="login-form account-loading-state">
      {error ? (
        <>
          <p className="auth-error" role="alert">{t(accountErrorKey(error))}</p>
          <p className="signup-copy"><Link href={error === "invalidCode" ? "/forgot-password" : "/login"}>{t(error === "invalidCode" ? "account.sendCode" : "common.signInAgain")}</Link></p>
        </>
      ) : <p role="status">{t("common.loading")}</p>}
    </div>
  );

  return (
    <form className="login-form" onSubmit={handleSubmit} aria-busy={saving}>
      <div className="account-identity">
        <span className="account-identity-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M4 6.5h16v11H4z"/><path d="m5 7.5 7 5 7-5"/></svg>
        </span>
        <span className="account-identity-copy"><small>{t("common.email")}</small><strong>{account.email}</strong></span>
        <span className="account-identity-status" aria-label={t("account.verifiedAccount")}>
          <svg aria-hidden="true" viewBox="0 0 20 20"><path d="m5 10 3 3 7-7"/></svg>
        </span>
      </div>

      {account.mode === "recovery" ? (
        <p className="auth-success" role="status">{t("account.emailVerified")}</p>
      ) : (
        <div className="field-group">
          <div className="field-label-row">
            <label htmlFor="current-password">{t("account.currentPassword")}</label>
            <Link href="/forgot-password">{t("common.forgotPassword")}</Link>
          </div>
          <div className="field-shell">
            <input id="current-password" name="currentPassword" type={visibleField === "current" ? "text" : "password"} maxLength={128} autoComplete="current-password" placeholder={t("auth.enterPassword")} disabled={saving} required />
            <button type="button" className="password-toggle" aria-label={t(visibleField === "current" ? "common.hide" : "common.show")} aria-pressed={visibleField === "current"} onClick={() => togglePassword("current")}>{t(visibleField === "current" ? "common.hide" : "common.show")}</button>
          </div>
        </div>
      )}

      <div className="field-group">
        <label htmlFor="password">{t("auth.newPassword")}</label>
        <div className="field-shell">
          <input id="password" name="password" type={visibleField === "new" ? "text" : "password"} minLength={8} maxLength={128} autoComplete="new-password" placeholder={t("auth.atLeastCharacters")} disabled={saving} required />
          <button type="button" className="password-toggle" aria-label={t(visibleField === "new" ? "common.hide" : "common.show")} aria-pressed={visibleField === "new"} onClick={() => togglePassword("new")}>{t(visibleField === "new" ? "common.hide" : "common.show")}</button>
        </div>
      </div>

      <div className="field-group">
        <label htmlFor="confirm-password">{t("account.confirmPassword")}</label>
        <div className="field-shell">
          <input id="confirm-password" name="confirmPassword" type={visibleField === "confirm" ? "text" : "password"} minLength={8} maxLength={128} autoComplete="new-password" placeholder={t("account.confirmPasswordPlaceholder")} disabled={saving} required />
          <button type="button" className="password-toggle" aria-label={t(visibleField === "confirm" ? "common.hide" : "common.show")} aria-pressed={visibleField === "confirm"} onClick={() => togglePassword("confirm")}>{t(visibleField === "confirm" ? "common.hide" : "common.show")}</button>
        </div>
      </div>

      <button className="submit-button" type="submit" disabled={saving}>{t(saving ? "account.savingPassword" : "auth.savePassword")}</button>
      {error && <p className="auth-error" role="alert">{t(accountErrorKey(error))}</p>}
      {error === "invalidCode" && <p className="signup-copy"><Link href="/forgot-password">{t("account.sendCode")}</Link></p>}
    </form>
  );
}
