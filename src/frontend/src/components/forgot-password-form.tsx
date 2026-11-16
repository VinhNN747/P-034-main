"use client";

import Link from "next/link";
import { type FormEvent, useEffect, useState } from "react";

import { useLanguage } from "@/components/language-provider";
import { AccountRequestError, accountErrorKey, clearRecoveryEmail, finishPasswordChange, postAccount, readRecoveryEmail, rememberRecoveryEmail } from "@/lib/account-client";

export function ForgotPasswordForm() {
  const [email, setEmail] = useState("");
  const [sentEmail, setSentEmail] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const cooldownActive = cooldown > 0;
  const { t } = useLanguage();

  useEffect(() => {
    // Restore only the email, never an OTP or password, after returning from a bad link.
    const savedEmail = readRecoveryEmail();
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (savedEmail) setEmail(savedEmail);
  }, []);

  useEffect(() => {
    if (!cooldownActive) return;
    const interval = window.setInterval(() => setCooldown((seconds) => Math.max(0, seconds - 1)), 1_000);
    return () => window.clearInterval(interval);
  }, [cooldownActive]);

  async function requestCode(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    if (loading || cooldownActive) return;
    setLoading(true);
    setError("");
    const normalizedEmail = (sentEmail ?? email).trim().toLowerCase();
    try {
      await postAccount("/api/account/recovery/request", { email: normalizedEmail });
      rememberRecoveryEmail(normalizedEmail);
      setSentEmail(normalizedEmail);
      setCooldown(60);
    } catch (cause) {
      setError(cause instanceof AccountRequestError ? cause.code : "recoveryUnavailable");
      if (cause instanceof AccountRequestError && cause.code === "rateLimited") setCooldown(60);
    } finally {
      setLoading(false);
    }
  }

  async function resetPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (loading || !sentEmail) return;
    const form = new FormData(event.currentTarget);
    const password = String(form.get("password") ?? "");
    const confirmPassword = String(form.get("confirmPassword") ?? "");
    if (password !== confirmPassword) { setError("passwordMismatch"); return; }
    setLoading(true);
    setError("");
    try {
      const result = await postAccount<{ saved: boolean; next?: string }>("/api/account/recovery/reset", {
        email: sentEmail, token: String(form.get("token") ?? "").trim(), password, confirmPassword,
      });
      finishPasswordChange(result.next);
    } catch (cause) {
      setError(cause instanceof AccountRequestError ? cause.code : "passwordUpdateFailed");
      setLoading(false);
    }
  }

  return <div className="login-form">
    {sentEmail ? <form className="login-form account-recovery-form" onSubmit={resetPassword} aria-busy={loading}>
      <p className="auth-success" role="status">{t("account.codeSentTo", { email: sentEmail })}</p>
      <div className="field-group"><label htmlFor="recovery-token">{t("account.verificationCode")}</label><div className="field-shell"><input id="recovery-token" name="token" type="text" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6,10}" minLength={6} maxLength={10} placeholder="000000" disabled={loading} required /></div></div>
      <div className="field-group"><label htmlFor="recovery-password">{t("auth.newPassword")}</label><div className="field-shell"><input id="recovery-password" name="password" type="password" minLength={8} maxLength={128} autoComplete="new-password" placeholder={t("auth.atLeastCharacters")} disabled={loading} required /></div></div>
      <div className="field-group"><label htmlFor="recovery-confirm-password">{t("account.confirmPassword")}</label><div className="field-shell"><input id="recovery-confirm-password" name="confirmPassword" type="password" minLength={8} maxLength={128} autoComplete="new-password" disabled={loading} required /></div></div>
      <button className="submit-button" type="submit" disabled={loading}>{t(loading ? "account.savingPassword" : "account.verifyAndReset")}</button>
      <div className="account-recovery-actions">
        <button type="button" className="account-secondary-button" disabled={loading || cooldownActive} onClick={() => void requestCode()}>{cooldownActive ? t("account.resendIn", { seconds: cooldown }) : t("account.resendCode")}</button>
        <button type="button" className="account-secondary-button" disabled={loading} onClick={() => { setSentEmail(null); setError(""); clearRecoveryEmail(); }}>{t("account.changeEmail")}</button>
      </div>
    </form> : <form className="login-form account-recovery-form" onSubmit={requestCode} aria-busy={loading}>
      <div className="field-group"><label htmlFor="email">{t("common.email")}</label><div className="field-shell"><input id="email" name="email" type="email" autoComplete="email" maxLength={254} placeholder="you@company.com" value={email} onChange={(event) => setEmail(event.target.value)} disabled={loading} required /></div></div>
      <button className="submit-button" type="submit" disabled={loading || cooldownActive}>{cooldownActive ? t("account.resendIn", { seconds: cooldown }) : t(loading ? "account.sendingCode" : "account.sendCode")}</button>
    </form>}
    {error && <p className="auth-error" role="alert">{t(accountErrorKey(error))}</p>}
    <p className="signup-copy"><Link href="/login">{t("common.backToSignIn")}</Link></p>
  </div>;
}
