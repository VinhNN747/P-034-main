"use client";

import Link from "next/link";

import { LanguageToggle } from "@/components/language-toggle";
import { useLanguage } from "@/components/language-provider";
import { ThemeToggle } from "@/components/theme-toggle";
import { TaskwayLogo } from "@/components/taskway-logo";

export function AccountPageHeader() {
  const { t } = useLanguage();

  return (
    <header className="login-header auth-header account-auth-header">
      <Link className="gateway-brand auth-brand" href="/dashboard" aria-label={t("account.backDashboard")}>
        <TaskwayLogo className="taskway-logo--auth" />
      </Link>
      <div className="account-auth-header-actions auth-header-actions">
        <LanguageToggle />
        <ThemeToggle compact />
        <Link className="quiet-link auth-home-link" href="/dashboard">
          <svg className="auth-back-arrow" aria-hidden="true" viewBox="0 0 20 20">
            <path d="m12.5 5-5 5 5 5" />
            <path d="M8 10h7" />
          </svg>
          <span>{t("account.backDashboard")}</span>
        </Link>
      </div>
    </header>
  );
}
