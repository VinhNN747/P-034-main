"use client";

import Link from "next/link";

import { AuthBackground } from "@/components/auth-background";
import { LanguageToggle } from "@/components/language-toggle";
import { LoginForm } from "@/components/login-form";
import { ThemeToggle } from "@/components/theme-toggle";
import { TaskwayLogo } from "@/components/taskway-logo";
import { useLanguage } from "@/components/language-provider";

export default function LoginPage() {
  const { t } = useLanguage();
  return (
    <main className="login-shell auth-shell">
      <AuthBackground />

      <header className="login-header auth-header">
        <Link className="gateway-brand auth-brand" href="/" aria-label={t("landing.home")}>
          <TaskwayLogo className="taskway-logo--auth" appearance="theme-aware" />
        </Link>
        <div className="auth-header-actions">
          <LanguageToggle />
          <ThemeToggle compact />
          <Link className="quiet-link auth-home-link" href="/">
          <span aria-hidden="true" className="auth-back-arrow">←</span> {t("common.backToHome")}
          </Link>
        </div>
      </header>

      <section className="login-layout auth-layout" aria-labelledby="login-heading">
        <aside className="login-context auth-context">
          <h1>
            {t("auth.signInToContinue")}{" "}
            <span className="auth-gradient-text">{t("auth.orchestratingAi")}</span>.
          </h1>

          <p>{t("auth.signInDescription")}</p>

          <div className="auth-login-logo">
            <TaskwayLogo appearance="theme-aware" />
            <p className="auth-login-tagline">© 2026 Taskway - The Fellowship of the Ring</p>
          </div>
        </aside>

        <section className="login-card auth-card-panel">
          <div className="auth-card-top-accent" aria-hidden="true" />
          <p className="section-label">{t("auth.welcomeBack")}</p>
          <h2 id="login-heading">{t("auth.signInWorkspace")}</h2>
          <p className="login-card-copy">{t("auth.manageDescription")}</p>
          <LoginForm />
        </section>
      </section>
    </main>
  );
}
