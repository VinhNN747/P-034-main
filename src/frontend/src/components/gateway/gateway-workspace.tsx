"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { GatewayScene } from "@/components/gateway/gateway-scenes";
import { useWorkspace } from "@/components/gateway/workspace-provider";
import { WorkspaceProfileMenu } from "@/components/gateway/workspace-profile-menu";
import { NotificationCenter } from "@/components/gateway/workspace-notifications";
import { LanguageToggle } from "@/components/language-toggle";
import { ThemeToggle } from "@/components/theme-toggle";
import { getNavigation, getRouteViewState } from "@/lib/gateway-navigation";
import { WorkspaceSkeleton } from "@/components/gateway/workspace-skeleton";
import { TaskwayLogo } from "@/components/taskway-logo";
import { useLanguage } from "@/components/language-provider";

type Props = { route: string; applicationId?: string; modelPoolId?: string; requestId?: string };

function AuthorizationState({ title, message, action }: { title: string; message: string; action: React.ReactNode }) {
  const { t } = useLanguage();
  return <main className="authorization-screen"><TaskwayLogo appearance="dark" className="taskway-logo--authorization" /><p className="gateway-eyebrow">{t("workspace.accessLabel")}</p><h1>{title}</h1><p>{message}</p><div className="authorization-actions">{action}</div></main>;
}

function Restricted() {
  const { t } = useLanguage();
  return <section className="gateway-empty-state"><div className="denied-code">403</div><p className="gateway-eyebrow">{t("workspace.accessDenied")}</p><h1>{t("workspace.restrictedTitle")}</h1><p>{t("workspace.restrictedDescription")}</p><Link href="/dashboard" className="gateway-button">{t("workspace.backToOverview")}</Link></section>;
}

function NavigationIcon({ href }: { href: string }) {
  if (href === "/dashboard") return <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></svg>;
  if (href === "/sdk-guide") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 9-3 3 3 3M16 9l3 3-3 3M14 5l-4 14" /></svg>;
  if (href === "/applications") return <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="15" rx="2" /><path d="M3 9h18M7 3v4M17 3v4" /></svg>;
  if (href === "/offline") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 4.5 6v5.5c0 4.4 3.1 8.1 7.5 9.5 4.4-1.4 7.5-5.1 7.5-9.5V6L12 3Z" /><path d="m9 12 2 2 4-4" /></svg>;
  if (href === "/models") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 8 4.5-8 4.5-8-4.5L12 3Z" /><path d="m4 12 8 4.5 8-4.5M4 16.5l8 4.5 8-4.5" /></svg>;
  if (href === "/providers") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3v4M16 3v4M6 7h12v3a6 6 0 0 1-6 6 6 6 0 0 1-6-6V7ZM12 16v5" /></svg>;
  if (href === "/model-pools") return <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="7" cy="7" r="3" /><circle cx="17" cy="7" r="3" /><circle cx="12" cy="17" r="3" /><path d="m9 9 2 5M15 9l-2 5" /></svg>;
  if (href === "/my-endpoints") return <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="7" rx="1.5" /><rect x="3" y="13" width="18" height="7" rx="1.5" /><path d="M7 7.5h.01M7 16.5h.01M11 7.5h6M11 16.5h6" /></svg>;
  if (href === "/requests") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h13M14 4l3 3-3 3M20 17H7M10 14l-3 3 3 3" /></svg>;
  if (href === "/usage") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></svg>;
  if (href === "/billing") return <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M3 10h18M7 15h4" /></svg>;
  if (href === "/incidents") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10.3 4.2 2.7 18a2 2 0 0 0 1.8 3h15a2 2 0 0 0 1.8-3L13.7 4.2a2 2 0 0 0-3.4 0Z" /><path d="M12 9v4M12 17h.01" /></svg>;
  return <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" /><path d="M12 8v8M8 12h8" /></svg>;
}

export function GatewayWorkspace({ route, applicationId, modelPoolId, requestId }: Props) {
  const { userId, authenticationComplete, roles, permissions, status, authorizationError, retryAuthorization } = useWorkspace();
  const { t } = useLanguage();
  const pathname = usePathname();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  function toggleSidebar() {
    setSidebarCollapsed((current) => !current);
  }

  if (status === "loading") return <WorkspaceSkeleton route={route} />;
  if (status === "unauthenticated") return <AuthorizationState title={t("workspace.sessionEnded")} message={t("workspace.sessionEndedDescription")} action={<Link className="gateway-button" href="/login">{t("common.signIn")}</Link>} />;
  if (status === "error") return <AuthorizationState title={t("workspace.accessCouldNotBeVerified")} message={`${authorizationError ?? t("workspace.authorizationUnavailable")} ${t("workspace.noProtectedData")}`} action={<><button className="gateway-button" type="button" onClick={retryAuthorization}>{t("common.tryAgain")}</button><Link className="gateway-button gateway-button--ghost" href="/login">{t("common.signInAgain")}</Link></>} />;
  if (!authenticationComplete) return <AuthorizationState title={t("account.finishRecovery")} message={t("account.finishRecoveryDescription")} action={<Link className="gateway-button" href="/update-password">{t("auth.newPassword")}</Link>} />;

  const navigation = getNavigation(permissions, roles);
  const restricted = getRouteViewState(route, permissions, roles) === "restricted";
  const global = permissions.includes("usage.read_global") || permissions.includes("requests.read_global");
  const activeRole = global || roles.includes("admin") ? t("workspace.admin") : roles.includes("user") ? t("workspace.user") : t("workspace.member");
  const isUser = roles.includes("user") && !roles.includes("admin") && !roles.includes("platform_admin");
  const groupItems = (group: "Workspace" | "Operate") => navigation.filter((item) => item.group === group);
  const navLabel = (label: string) => {
    const keyByLabel: Record<string, Parameters<typeof t>[0]> = {
      Overview: "nav.overview", "SDK Guide": "nav.sdkGuide", Applications: "nav.applications", Offline: "nav.offline", Models: "nav.models",
      Providers: "nav.providers", "Model pools": "nav.modelPools", Requests: "nav.requests", Usage: "nav.usage",
      Billing: "nav.billing", Incidents: "nav.incidents",
    };
    return keyByLabel[label] ? t(keyByLabel[label]) : label;
  };
  const isActive = (href: string) => pathname === href || (["/applications", "/offline", "/reports", "/incidents"].includes(href) && pathname.startsWith(href + "/"))
    || (href === "/offline" && pathname === "/my-endpoints");

  return <div className={`gateway-shell${sidebarCollapsed ? " gateway-shell--sidebar-collapsed" : ""}`}>
    <a className="workspace-skip-link" href="#main-content">{t("workspace.skipToContent")}</a>
    <aside className={`gateway-sidebar${sidebarCollapsed ? " gateway-sidebar--collapsed" : ""}`}>
      <div className="gateway-sidebar-scroll" id="workspace-navigation">
        <div className="gateway-sidebar-header">
          {!sidebarCollapsed && <Link href="/dashboard" className="gateway-brand workspace-brand" aria-label={t("workspace.taskwayAria")}>
            <span className="workspace-brand-logo-frame">
              <TaskwayLogo appearance="theme-aware" className="taskway-logo--workspace" />
            </span>
          </Link>}
          <button type="button" className="gateway-sidebar-toggle" onClick={toggleSidebar} aria-expanded={!sidebarCollapsed} aria-controls="workspace-navigation" aria-label={sidebarCollapsed ? t("workspace.expandSidebar") : t("workspace.collapseSidebar")} title={sidebarCollapsed ? t("workspace.expandSidebar") : t("workspace.collapseSidebar")}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="4" width="17" height="16" rx="2.5" /><path d="M9 4v16" />{sidebarCollapsed ? <path d="m13 9 3 3-3 3" /> : <path d="m16 9-3 3 3 3" />}</svg>
          </button>
        </div>
        <div className="gateway-workspace-label" aria-label={`${t("workspace.active")}: ${activeRole}`} title={sidebarCollapsed ? activeRole : undefined}>{sidebarCollapsed ? <span className="gateway-workspace-badge" aria-hidden="true">{activeRole.charAt(0)}</span> : <span className="gateway-workspace-copy"><small>{t("workspace.active")}</small><strong>{activeRole}</strong></span>}</div>
        {(["Workspace", "Operate"] as const).map((group) => groupItems(group).length > 0 && <nav className="gateway-nav-group" aria-label={t(group === "Workspace" ? "nav.workspace" : "nav.operate")} key={group}>{!sidebarCollapsed && <p>{t(group === "Workspace" ? "nav.workspace" : "nav.operate")}</p>}{groupItems(group).map((item) => <Link key={item.href} href={item.href} aria-label={sidebarCollapsed ? navLabel(item.label) : undefined} title={sidebarCollapsed ? navLabel(item.label) : undefined} aria-current={isActive(item.href) ? "page" : undefined} className={isActive(item.href) ? "is-active" : ""}><span className="gateway-nav-icon"><NavigationIcon href={item.href} /></span>{!sidebarCollapsed && <span className="gateway-nav-label">{navLabel(item.label)}</span>}</Link>)}</nav>)}
      </div>
    </aside>
    <main className="gateway-main" id="main-content">
      <header className="gateway-topbar"><div className="gateway-topbar-actions">
        <div className="workspace-header-preferences" role="group" aria-label={t("landing.preferenceControls")}>
          {isUser && <NotificationCenter key={userId} />}
          <ThemeToggle compact />
          <LanguageToggle />
        </div>
        <WorkspaceProfileMenu isUser={isUser} />
      </div></header>
      <div className="gateway-content">{restricted ? <Restricted /> : <GatewayScene route={route} applicationId={applicationId} modelPoolId={modelPoolId} requestId={requestId} permissions={permissions} />}</div>
    </main>
  </div>;
}
