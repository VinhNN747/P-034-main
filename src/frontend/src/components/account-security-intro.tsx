"use client";

import { useLanguage } from "@/components/language-provider";

export function AccountSecurityIntro() {
  const { t } = useLanguage();

  return (
    <aside className="login-context auth-context account-security-intro">
      <div className="auth-badge-pod">
        <span className="auth-live-dot" aria-hidden="true">
          <span className="auth-live-ping" />
        </span>
        <span>{t("account.securityBadge")}</span>
      </div>

      <h1>
        {t("account.securityTitle")} {" "}
        <span className="auth-gradient-text">{t("account.securityTitleAccent")}</span>.
      </h1>
      <p>{t("account.securityIntro")}</p>

      <div className="account-security-panel">
        <div className="account-security-panel-head">
          <span>{t("account.securityProcess")}</span>
          <span className="account-security-state">
            <i aria-hidden="true" />
            {t("account.protected")}
          </span>
        </div>
        <ol>
          <li>
            <span className="account-security-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M12 3 5.5 5.7v5.5c0 4.4 2.7 7.9 6.5 9.8 3.8-1.9 6.5-5.4 6.5-9.8V5.7L12 3Z"/><path d="m9.2 12 1.8 1.8 3.9-4"/></svg>
            </span>
            <span><strong>{t("account.verifyIdentity")}</strong><small>{t("account.verifyIdentityDescription")}</small></span>
          </li>
          <li>
            <span className="account-security-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24"><rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8.5 10V7.5a3.5 3.5 0 0 1 7 0V10"/><path d="M12 14v2.5"/></svg>
            </span>
            <span><strong>{t("account.secureSession")}</strong><small>{t("account.secureSessionDescription")}</small></span>
          </li>
          <li>
            <span className="account-security-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M7 18.5c-1.7 0-3-1.1-3-2.5s1.3-2.5 3-2.5c.3 0 .6 0 .9.1A4.5 4.5 0 0 1 16.5 12H17c1.7 0 3 1.2 3 2.8s-1.3 2.7-3 2.7H7Z"/><path d="M9 17.5v2M15 17.5v2M12 17.5V21"/></svg>
            </span>
            <span><strong>{t("account.privateData")}</strong><small>{t("account.privateDataDescription")}</small></span>
          </li>
        </ol>
        <div className="account-security-panel-foot">
          <span>{t("account.encryptedVerification")}</span>
          <span>{t("account.accountScoped")}</span>
        </div>
      </div>
    </aside>
  );
}
