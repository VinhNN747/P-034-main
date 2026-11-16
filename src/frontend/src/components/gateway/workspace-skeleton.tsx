"use client";

import { GatewayPageSkeleton } from "@/components/gateway/gateway-page-skeletons";
import { TaskwayLogo } from "@/components/taskway-logo";
import { useLanguage } from "@/components/language-provider";

const navigationRows = ["Overview", "Applications", "Models", "Usage"];

/** A layout-preserving placeholder while the workspace access profile is loading. */
export function WorkspaceSkeleton({ route = "/dashboard" }: { route?: string }) {
  const { t } = useLanguage();
  return (
    <div className="gateway-shell workspace-skeleton" aria-busy="true" aria-live="polite">
      <aside className="gateway-sidebar" aria-hidden="true">
        <div className="gateway-brand workspace-brand">
          <span className="workspace-brand-logo-frame">
            <TaskwayLogo appearance="theme-aware" className="taskway-logo--workspace" />
          </span>
        </div>
        <div className="skeleton-block skeleton-workspace-label" />
        <div className="skeleton-nav-group">
          <span className="skeleton-block skeleton-nav-title" />
          {navigationRows.map((row) => <span className="skeleton-block skeleton-nav-row" key={row} />)}
        </div>
        <div className="skeleton-sidebar-footer">
          <span className="skeleton-block skeleton-footer-line" />
          <span className="skeleton-block skeleton-footer-line skeleton-footer-line--short" />
        </div>
      </aside>

      <main className="gateway-main" id="main-content">
        <header className="gateway-topbar" aria-hidden="true">
          <span className="skeleton-block skeleton-environment" />
          <span className="skeleton-block skeleton-user-control" />
        </header>
        <div className="gateway-content">
          <p className="skeleton-status">{t("workspace.loading")}</p>
          <GatewayPageSkeleton route={route} />
        </div>
      </main>
      <span className="sr-only">{t("workspace.loadingDescription")}</span>
    </div>
  );
}
