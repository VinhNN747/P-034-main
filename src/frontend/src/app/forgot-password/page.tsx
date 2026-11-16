"use client";

import { AuthBackground } from "@/components/auth-background";
import { AccountPageHeader } from "@/components/account-page-header";
import { AccountSecurityIntro } from "@/components/account-security-intro";
import { ForgotPasswordForm } from "@/components/forgot-password-form";
import { useLanguage } from "@/components/language-provider";

export default function ForgotPasswordPage() {
  const { t } = useLanguage();
  return (
    <main className="login-shell auth-shell account-auth-shell">
      <AuthBackground />
      <AccountPageHeader />

      <section className="login-layout auth-layout account-auth-layout" aria-labelledby="forgot-password-heading">
        <AccountSecurityIntro />
        <section className="login-card auth-card-panel account-auth-card">
          <div className="auth-card-top-accent" aria-hidden="true" />
          <p className="section-label">{t("auth.accountRecovery")}</p>
          <h2 id="forgot-password-heading">{t("auth.forgotPassword")}</h2>
          <p className="login-card-copy">{t("auth.forgotPasswordDescription")}</p>
          <ForgotPasswordForm />
        </section>
      </section>
    </main>
  );
}
