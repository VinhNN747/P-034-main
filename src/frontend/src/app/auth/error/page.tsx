"use client";

import Link from "next/link";
import { useLanguage } from "@/components/language-provider";

export default function AuthErrorPage() {
  const { t } = useLanguage();
  return <main className="auth-page"><section className="auth-card">
    <p className="eyebrow">{t("auth.confirmEyebrow")}</p>
    <h1>{t("account.invalidLink")}</h1>
    <p className="auth-description">{t("auth.invalidConfirmationDescription")}</p>
    <div className="login-form">
      <Link className="submit-button" href="/sign-up">{t("auth.signupReturn")}</Link>
      <Link className="account-secondary-button" href="/forgot-password">{t("auth.forgotPassword")}</Link>
      <p className="signup-copy"><Link href="/login">{t("common.backToSignIn")}</Link></p>
    </div>
  </section></main>;
}
