"use client";

import { useLanguage } from "@/components/language-provider";
import styles from "./landing-application-preview.module.css";

const RECENT_REQUESTS = [
  { id: "req_faq_7A31", source: "Playground", time: "Oct 14 · 10:42", metrics: "184 ms / 0.0012" },
  { id: "req_faq_4D08", source: "API key", time: "Oct 14 · 10:38", metrics: "236 ms / 0.0015" },
  { id: "req_faq_9C52", source: "Playground", time: "Oct 14 · 10:24", metrics: "192 ms / 0.0011" },
] as const;

function WorkspaceGlyph({ kind }: { kind: "overview" | "applications" | "pools" | "requests" | "usage" }) {
  if (kind === "overview") return <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="3" y="3" width="5" height="5" rx="1" /><rect x="12" y="3" width="5" height="5" rx="1" /><rect x="3" y="12" width="5" height="5" rx="1" /><rect x="12" y="12" width="5" height="5" rx="1" /></svg>;
  if (kind === "applications") return <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="3" y="3" width="14" height="14" rx="2" /><path d="M3 8h14M8 8v9" /></svg>;
  if (kind === "pools") return <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="6" cy="6" r="2.5" /><circle cx="14" cy="6" r="2.5" /><circle cx="10" cy="14" r="2.5" /><path d="m8 7.5 1 4M12 11.5l1-4M8.5 6h3" /></svg>;
  if (kind === "requests") return <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 3.5h7l3 3V16a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1Z" /><path d="M12 3.5v3h3M7 10h6M7 13h6" /></svg>;
  return <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 16V9M8 16V5M13 16v-4M18 16V7" /><path d="M2 16.5h17" /></svg>;
}

function ApplicationPreviewSidebar({
  workspace,
  operate,
  activeWorkspace,
  role,
}: {
  workspace: string;
  operate: string;
  activeWorkspace: string;
  role: string;
}) {
  const { t } = useLanguage();

  return (
    <aside className={`${styles.sidebar} gateway-sidebar`} aria-label={workspace}>
      <div className={`${styles.brand} gateway-brand`}>
        <span className={styles.brandMark} aria-hidden="true">T</span>
        <strong>Taskway</strong>
      </div>

      <div className={`${styles.workspaceLabel} gateway-workspace-label`}>
        <small>{activeWorkspace}</small>
        <strong>{role}</strong>
        <span>Taskway AI</span>
      </div>

      <nav className={`${styles.navigation} gateway-nav-group`} aria-label={workspace}>
        <p>{workspace}</p>
        <span><WorkspaceGlyph kind="overview" />{t("nav.overview")}</span>
        <span className={`${styles.activeNav} is-active`} aria-current="page"><WorkspaceGlyph kind="applications" />{t("nav.applications")}</span>
        <span><WorkspaceGlyph kind="pools" />{t("nav.modelPools")}</span>
      </nav>

      <nav className={`${styles.navigation} gateway-nav-group`} aria-label={operate}>
        <p>{operate}</p>
        <span><WorkspaceGlyph kind="requests" />{t("nav.requests")}</span>
        <span><WorkspaceGlyph kind="usage" />{t("nav.usage")}</span>
      </nav>

      <div className={styles.sidebarFooter}>
        <i aria-hidden="true" />
        <span>Taskway Gateway</span>
      </div>
    </aside>
  );
}

export function LandingApplicationLoading() {
  const { language } = useLanguage();
  const copy = language === "vi"
    ? {
        workspace: "Workspace",
        operate: "Vận hành",
        activeWorkspace: "Workspace đang dùng",
        role: "Quản trị viên",
        loading: "Đang tải Applications…",
      }
    : {
        workspace: "Workspace",
        operate: "Operate",
        activeWorkspace: "Active workspace",
        role: "Administrator",
        loading: "Loading Applications…",
      };

  return (
    <div className={`${styles.preview} ${styles.loadingPreview} gateway-shell`} data-application-loading aria-busy="true" aria-live="polite">
      <ApplicationPreviewSidebar
        workspace={copy.workspace}
        operate={copy.operate}
        activeWorkspace={copy.activeWorkspace}
        role={copy.role}
      />

      <main className={`${styles.main} gateway-main`}>
        <header className={`${styles.topbar} gateway-topbar`} aria-hidden="true">
          <div className={styles.loadingToolbar}>
            <span className={`${styles.loadingBlock} ${styles.loadingToolbarIcon}`} />
            <span className={`${styles.loadingBlock} ${styles.loadingToolbarIcon}`} />
            <span className={`${styles.loadingBlock} ${styles.loadingToolbarLanguage}`} />
            <span className={`${styles.loadingBlock} ${styles.loadingToolbarProfile}`} />
          </div>
        </header>

        <div className={`${styles.content} ${styles.loadingContent} gateway-content`}>
          <div className={styles.loadingPageHeading} aria-hidden="true">
            <div className={styles.loadingHeadingCopy}>
              <span className={`${styles.loadingBlock} ${styles.loadingEyebrow}`} />
              <span className={`${styles.loadingBlock} ${styles.loadingTitle}`} />
              <span className={`${styles.loadingBlock} ${styles.loadingDescription}`} />
              <span className={`${styles.loadingBlock} ${styles.loadingDescriptionShort}`} />
            </div>
            <span className={`${styles.loadingBlock} ${styles.loadingAction}`} />
          </div>

          <div className={styles.loadingFilterCard} aria-hidden="true" />

          <div className={styles.loadingTable} aria-hidden="true">
            {Array.from({ length: 6 }, (_, row) => (
              <div className={styles.loadingTableRow} key={row}>
                {Array.from({ length: 6 }, (_, column) => (
                  <span
                    className={`${styles.loadingBlock} ${styles.loadingTableCell} ${column % 2 === 1 ? styles.loadingTableCellWide : ""}`}
                    key={column}
                  />
                ))}
              </div>
            ))}
          </div>

          <span className="sr-only">{copy.loading}</span>
        </div>
      </main>
    </div>
  );
}

export function LandingApplicationPreview({ onBack }: { onBack: () => void }) {
  const { language, t } = useLanguage();
  const copy = language === "vi"
    ? {
        workspace: "Workspace",
        operate: "Vận hành",
        activeWorkspace: "Workspace đang dùng",
        role: "Quản trị viên",
        production: "production application",
        configVersion: "Configuration version 12",
        back: "Quay lại chatbot",
        objective: "Routing objective",
        routingNote: "Routing policy",
        models: "Available models",
        enabled: "2 enabled",
        budget: "Budget guardrail",
        monthly: "Monthly limit",
        requests: "Requests",
        recorded: "Đã ghi nhận",
        recentWindow: "3 requests · last 24 hours",
        completed: "completed",
      }
    : {
        workspace: "Workspace",
        operate: "Operate",
        activeWorkspace: "Active workspace",
        role: "Administrator",
        production: "production application",
        configVersion: "Configuration version 12",
        back: "Back to chatbot",
        objective: "Routing objective",
        routingNote: "Routing policy",
        models: "Available models",
        enabled: "2 enabled",
        budget: "Budget guardrail",
        monthly: "Monthly limit",
        requests: "Requests",
        recorded: "Recorded usage",
        recentWindow: "3 requests · last 24 hours",
        completed: "completed",
      };

  return (
    <div className={`${styles.preview} gateway-shell`} data-application-preview>
      <ApplicationPreviewSidebar
        workspace={copy.workspace}
        operate={copy.operate}
        activeWorkspace={copy.activeWorkspace}
        role={copy.role}
      />

      <main className={`${styles.main} gateway-main`}>
        <header className={`${styles.topbar} gateway-topbar`}>
          <div className={styles.topbarPath}><span>{t("nav.applications")}</span><i aria-hidden="true">/</i><strong>FAQ Assistant</strong></div>
          <span className={`${styles.environment} environment-badge`}>production</span>
        </header>

        <div className={`${styles.content} gateway-content`}>
          <button className={styles.backButton} onClick={onBack} type="button">
            <span aria-hidden="true">←</span>{copy.back}
          </button>

          <section className={`${styles.heading} gateway-page-heading`}>
            <div>
              <p className="gateway-eyebrow">{copy.production}</p>
              <h1>FAQ Assistant</h1>
              <p>faq-assistant <span aria-hidden="true">·</span> {copy.configVersion}</p>
            </div>
          </section>

          <nav className={`${styles.tabs} application-tabs`} aria-label={t("applications.configuration")}>
            <span className={`${styles.activeTab} is-active`} aria-current="page">{t("nav.overview")}</span>
            <span>Playground</span>
            <span>Settings</span>
          </nav>

          <section className={`${styles.policyGrid} gateway-policy-grid`} aria-label={t("nav.overview")}>
            <article>
              <span>{copy.objective}</span>
              <strong>Balanced</strong>
              <small>{copy.routingNote}</small>
            </article>
            <article>
              <span>{copy.models}</span>
              <strong>{copy.enabled}</strong>
              <small>GPT-4.1 · Claude Sonnet</small>
            </article>
            <article>
              <span>{copy.budget}</span>
              <strong>250.00</strong>
              <small>{copy.monthly}</small>
            </article>
            <article>
              <span>{copy.requests}</span>
              <strong>12,840</strong>
              <small>{copy.recorded}</small>
            </article>
          </section>

          <section className={`${styles.requests} gateway-panel application-recent-requests`} aria-labelledby="landing-preview-recent-requests">
            <div className={`${styles.sectionHeading} gateway-section-heading`}>
              <div>
                <h2 id="landing-preview-recent-requests">{t("applications.recentRequests")}</h2>
                <p>{copy.recentWindow}</p>
              </div>
              <span aria-hidden="true">•••</span>
            </div>
            <div className={styles.requestList} role="list">
              {RECENT_REQUESTS.map((request) => (
                <div className={styles.requestRow} key={request.id} role="listitem">
                  <div className={styles.requestIdentity}>
                    <strong>{request.id}</strong>
                    <small>{request.source} <span aria-hidden="true">/</span> {request.time}</small>
                  </div>
                  <div className={styles.requestResult}>
                    <span className="gateway-status gateway-status--healthy"><i aria-hidden="true" />{copy.completed}</span>
                    <small>{request.metrics}</small>
                  </div>
                </div>
              ))}
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}
