"use client";

import { AuthBackground } from "@/components/auth-background";
import { AccountPageHeader } from "@/components/account-page-header";
import { AccountSecurityIntro } from "@/components/account-security-intro";
import { UpdatePasswordForm } from "@/components/update-password-form";
import { useLanguage } from "@/components/language-provider";

export default function UpdatePasswordPage() {
  const { t } = useLanguage();
  return (
    <main className="login-shell auth-shell account-auth-shell">
      <AuthBackground />
      <AccountPageHeader />

      <section className="login-layout auth-layout account-auth-layout" aria-labelledby="change-password-heading">
        <AccountSecurityIntro />
        <section className="login-card auth-card-panel account-auth-card">
          <div className="auth-card-top-accent" aria-hidden="true" />
          <p className="section-label">{t("auth.securityLabel")}</p>
          <h2 id="change-password-heading">{t("profile.changePassword")}</h2>
          <p className="login-card-copy">{t("account.changePasswordDescription")}</p>
          <UpdatePasswordForm />
        </section>
      </section>
    </main>
  );
}
