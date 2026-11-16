"use client";

import Link from "next/link";

import { AuthBackground } from "@/components/auth-background";
import { LanguageToggle } from "@/components/language-toggle";
import { SignUpForm } from "@/components/sign-up-form";
import { ThemeToggle } from "@/components/theme-toggle";
import { TaskwayLogo } from "@/components/taskway-logo";
import { useLanguage } from "@/components/language-provider";

export default function SignUpPage() {
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

      <section className="login-layout auth-layout" aria-labelledby="sign-up-heading">
        <aside className="login-context auth-context">
          <div className="auth-badge-pod">
            <span className="auth-live-dot" aria-hidden="true">
              <span className="auth-live-ping" />
            </span>
            <span>{t("auth.getStartedGateway")}</span>
          </div>

          <h1>
            {t("auth.oneAccount")}{" "}
            <span className="auth-gradient-text">{t("auth.aiInfrastructure")}</span>.
          </h1>

          <p>
            {t("auth.signUpDescription")}
          </p>

          <div className="auth-onboarding" aria-label={t("auth.onboardingAria")}>
            <div className="auth-onboarding-head">
              <span className="status-radar" aria-hidden="true">
                <span className="status-radar-ping" />
                <span className="status-radar-core" />
              </span>
              <span className="trace-status-label">{t("auth.workspaceSetup")}</span>
              <span className="trace-id">{t("auth.startHere")}</span>
            </div>
            <ol>
              <li>
                <b>01</b>
                <span><strong>{t("auth.createYourWorkspace")}</strong><small>{t("auth.setUpTeamSpace")}</small></span>
              </li>
              <li>
                <b>02</b>
                <span><strong>{t("auth.connectProvider")}</strong><small>{t("auth.addProviderCredentials")}</small></span>
              </li>
              <li>
                <b>03</b>
                <span><strong>{t("auth.connectYourApplication")}</strong><small>{t("auth.pointSdkToGateway")}</small></span>
              </li>
            </ol>
            <div className="auth-onboarding-foot">
              <span>{t("auth.workspaceScopedKeys")}</span>
              <span>{t("auth.openAiCompatibleApi")}</span>
            </div>
          </div>
        </aside>

        <section className="login-card auth-card-panel">
          <div className="auth-card-top-accent" aria-hidden="true" />
          <p className="section-label">{t("auth.createWorkspace")}</p>
          <h2 id="sign-up-heading">{t("auth.getStartedTaskway")}</h2>
          <p className="login-card-copy">{t("auth.createAccountDescription")}</p>
          <SignUpForm />
        </section>
      </section>
    </main>
  );
}
