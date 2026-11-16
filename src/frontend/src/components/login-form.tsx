"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { useWorkspace } from "@/components/gateway/workspace-provider";
import { useLanguage } from "@/components/language-provider";
import { createClient } from "@/lib/supabase/client";
import { signInVerified, SignupError } from "@/lib/signup";

export function LoginForm() {
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [verificationRequired, setVerificationRequired] = useState(false);
  const { startAuthorization } = useWorkspace();
  const { t } = useLanguage();
  const router = useRouter();

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setVerificationRequired(false);
    setLoading(true);
    const formData = new FormData(event.currentTarget);
    const supabase = createClient();
    const email = String(formData.get("email") ?? "").trim().toLowerCase();
    try {
      await signInVerified(supabase.auth, email, String(formData.get("password") ?? ""));
      startAuthorization();
      router.replace("/dashboard");
    } catch (cause) {
      const pending = cause instanceof SignupError && cause.key === "auth.signupVerifyFirst";
      setVerificationRequired(pending);
      setError(t(pending ? "auth.signupVerifyFirst" : "auth.incorrectCredentials"));
      if (pending) {
        try { window.sessionStorage.setItem("taskway:signup-email", email); } catch { /* Storage is optional. */ }
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <form className="login-form" method="post" onSubmit={handleSubmit}>
      <div className="field-group">
        <label htmlFor="email">{t("common.email")}</label>
        <div className="field-shell">
          <input id="email" name="email" type="email" autoComplete="email" placeholder="you@company.com" required />
        </div>
      </div>
      <div className="field-group">
        <label htmlFor="password">{t("common.password")}</label>
        <div className="field-shell">
          <input id="password" name="password" type={showPassword ? "text" : "password"} autoComplete="current-password" placeholder={t("auth.enterPassword")} required />
          <button type="button" className="password-toggle" aria-label={showPassword ? t("common.hide") : t("common.show")} aria-pressed={showPassword} onClick={() => setShowPassword((visible) => !visible)}>
            {showPassword ? t("common.hide") : t("common.show")}
          </button>
        </div>
        <div className="field-assistance"><Link href="/forgot-password">{t("common.forgotPassword")}</Link></div>
      </div>
      <button className="submit-button" type="submit" disabled={loading}>{loading ? t("common.signingIn") : t("common.signIn")}</button>
      {error && <p className="auth-error" role="alert">{error}</p>}
      {verificationRequired && <p className="signup-copy"><Link href="/sign-up?verify=1">{t("auth.signupReturn")}</Link></p>}
      <div className="form-divider" aria-hidden="true"><span /></div>
      <p className="signup-copy">{t("auth.newToTaskway")} <Link href="/sign-up">{t("auth.createAccount")}</Link></p>
    </form>
  );
}
