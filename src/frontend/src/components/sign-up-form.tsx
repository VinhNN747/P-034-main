"use client";

import Link from "next/link";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { createClient } from "@/lib/supabase/client";
import { requestSignup, resendSignup, signupErrorKey, verifySignup } from "@/lib/signup";
import { useLanguage } from "@/components/language-provider";

const pendingEmailKey = "taskway:signup-email";

function rememberEmail(email: string | null) {
  // Store only the pending email, never a password or OTP.
  try {
    if (email) window.sessionStorage.setItem(pendingEmailKey, email);
    else window.sessionStorage.removeItem(pendingEmailKey);
  } catch { /* Storage is optional. */ }
}

export function SignUpForm() {
  const [showPassword, setShowPassword] = useState(false);
  const [sentEmail, setSentEmail] = useState<string | null>(null);
  const [manualVerification, setManualVerification] = useState(false);
  const [verificationEmail, setVerificationEmail] = useState("");
  const [verified, setVerified] = useState(false);
  const [resendAccepted, setResendAccepted] = useState(false);
  const [error, setError] = useState<ReturnType<typeof signupErrorKey> | "">("");
  const [loading, setLoading] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const busy = useRef(false);
  const emailInput = useRef<HTMLInputElement>(null);
  const cooldownActive = cooldown > 0;
  const { t } = useLanguage();

  useEffect(() => {
    // Email buttons navigate here without redeeming any token. This also
    // works in another browser where the pending email was never stored.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setManualVerification(new URLSearchParams(window.location.search).get("verify") === "1");
    try {
      const saved = window.sessionStorage.getItem(pendingEmailKey);
      if (saved) setSentEmail(saved);
    } catch { /* Storage is optional. */ }
  }, []);

  useEffect(() => {
    if (!cooldownActive) return;
    const interval = window.setInterval(() => setCooldown((seconds) => Math.max(0, seconds - 1)), 1_000);
    return () => window.clearInterval(interval);
  }, [cooldownActive]);

  async function run(action: () => Promise<void>) {
    if (busy.current) return;
    busy.current = true;
    setError("");
    setResendAccepted(false);
    setLoading(true);
    try { await action(); }
    catch (cause) {
      const key = signupErrorKey(cause);
      setError(key);
      if (key === "account.error.rateLimited") setCooldown(60);
    } finally {
      busy.current = false;
      setLoading(false);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    if (cooldownActive) return;
    await run(async () => {
      const email = await requestSignup(createClient().auth, String(values.get("email") ?? ""), String(values.get("password") ?? ""));
      form.reset();
      rememberEmail(email);
      setSentEmail(email);
      setCooldown(60);
    });
  }

  async function handleVerify(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const email = sentEmail ?? verificationEmail;
    if (!email) return;
    const token = String(new FormData(event.currentTarget).get("token") ?? "");
    await run(async () => {
      await verifySignup(createClient().auth, email, token);
      rememberEmail(null);
      setVerified(true);
    });
  }

  if (verified) return <div className="login-form">
    <p className="auth-success" role="status">{t("auth.signupSuccess")}</p>
    <Link className="submit-button" href="/dashboard">{t("auth.signupContinue")}</Link>
  </div>;

  return <div className="login-form" data-signup-flow="otp-v2">
    {sentEmail || manualVerification ? <form className="login-form" onSubmit={handleVerify} aria-busy={loading}>
      <p className="auth-success" role="status">{sentEmail ? t("auth.signupCodeSent", { email: sentEmail }) : t("auth.signupEnterCode")}</p>
      {!sentEmail && <div className="field-group">
        <label htmlFor="verification-email">{t("common.email")}</label>
        <div className="field-shell">
          <input ref={emailInput} id="verification-email" name="email" type="email" autoComplete="email" maxLength={254} placeholder="you@company.com" value={verificationEmail} onChange={(event) => setVerificationEmail(event.target.value)} disabled={loading} required />
        </div>
      </div>}
      <div className="field-group">
        <label htmlFor="sign-up-token">{t("account.verificationCode")}</label>
        <div className="field-shell">
          <input id="sign-up-token" name="token" type="text" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6,10}" minLength={6} maxLength={10} placeholder="000000" disabled={loading} required autoFocus />
        </div>
      </div>
      <button className="submit-button" type="submit" disabled={loading}>{t(loading ? "auth.signupVerifying" : "auth.signupVerify")}</button>
      <div className="account-recovery-actions">
        <button type="button" className="account-secondary-button" disabled={loading || cooldownActive} onClick={() => {
          if (cooldownActive) return;
          if (!sentEmail && !emailInput.current?.reportValidity()) return;
          const email = (sentEmail ?? verificationEmail).trim().toLowerCase();
          void run(async () => {
            await resendSignup(email);
            rememberEmail(email);
            setSentEmail(email);
            setCooldown(60);
            setResendAccepted(true);
          });
        }}>{cooldownActive ? t("account.resendIn", { seconds: cooldown }) : t("account.resendCode")}</button>
        <button type="button" className="account-secondary-button" disabled={loading} onClick={() => {
          rememberEmail(null);
          setSentEmail(null);
          setVerificationEmail("");
          setError("");
          setResendAccepted(false);
          // Stay in OTP mode after opening an email on another device.
        }}>{t("account.changeEmail")}</button>
      </div>
      {resendAccepted && <p className="auth-success" role="status">{t("auth.signupResendAccepted")}</p>}
      {manualVerification && <p className="signup-copy"><Link href="/sign-up" onClick={() => { setManualVerification(false); setSentEmail(null); rememberEmail(null); setResendAccepted(false); }}>{t("common.createAccount")}</Link></p>}
    </form> : <form className="login-form" onSubmit={handleSubmit} aria-busy={loading}>
      <div className="field-group">
        <label htmlFor="sign-up-email">{t("common.email")}</label>
        <div className="field-shell">
          <input id="sign-up-email" name="email" type="email" autoComplete="email" maxLength={254} placeholder="you@company.com" disabled={loading} required />
        </div>
      </div>
      <div className="field-group">
        <div className="field-label-row">
          <label htmlFor="sign-up-password">{t("common.password")}</label>
          <span>{t("auth.atLeastCharacters")}</span>
        </div>
        <div className="field-shell">
          <input id="sign-up-password" name="password" type={showPassword ? "text" : "password"} autoComplete="new-password" placeholder={t("auth.createPassword")} minLength={8} maxLength={128} disabled={loading} required />
          <button type="button" className="password-toggle" aria-label={showPassword ? t("common.hide") : t("common.show")} aria-pressed={showPassword} onClick={() => setShowPassword((visible) => !visible)}>
            {showPassword ? t("common.hide") : t("common.show")}
          </button>
        </div>
      </div>
      <button className="submit-button" type="submit" disabled={loading || cooldownActive}>{cooldownActive ? t("account.resendIn", { seconds: cooldown }) : t(loading ? "account.sendingCode" : "auth.signupSendCode")}</button>
    </form>}
    {error && <p className="auth-error" role="alert">{t(error)}</p>}
    <div className="form-divider" aria-hidden="true"><span /></div>
    <p className="signup-copy">{t("auth.alreadyHaveAccount")} <Link href="/login">{t("common.signIn")}</Link></p>
  </div>;
}
