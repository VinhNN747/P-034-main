"use client";

import { useRef } from "react";
import { gsap } from "gsap";
import { useGSAP } from "@gsap/react";

import { TaskwayLogo } from "@/components/taskway-logo";
import styles from "./landing-product-walkthrough.module.css";

gsap.registerPlugin(useGSAP);

const steps = [
  "Create application",
  "Generate API key",
  "Run a Playground request",
  "Review request trace",
];
const POINTER_SETTLE_DURATION = 0.18;

function pointerPosition(root: HTMLElement, selector: string, axis: "x" | "y") {
  return () => {
    const target = root.querySelector<HTMLElement>(selector);
    const stage = root.querySelector<HTMLElement>("[data-walkthrough-canvas]");
    const cursor = root.querySelector<HTMLElement>("[data-walkthrough-cursor]");
    if (!target || !stage || !cursor) return 0;

    const targetRect = target.getBoundingClientRect();
    // GSAP positions the cursor relative to its offset parent (.screenHost),
    // so target coordinates must use that same origin instead of the workspace.
    const coordinateSpace = cursor.offsetParent instanceof HTMLElement ? cursor.offsetParent : stage;
    const coordinateRect = coordinateSpace.getBoundingClientRect();
    const cursorWidth = cursor.offsetWidth;
    const cursorHeight = cursor.offsetHeight;
    return axis === "x"
      ? targetRect.left - coordinateRect.left + targetRect.width / 2 + cursorWidth * (0.5 - 3 / 22)
      : targetRect.top - coordinateRect.top + targetRect.height / 2 + cursorHeight * (0.5 - 2.6 / 26);
  };
}

function movePointer(timeline: gsap.core.Timeline, root: HTMLElement, target: string, duration = 0.56) {
  const cursor = root.querySelector<HTMLElement>("[data-walkthrough-cursor]");
  if (!cursor) return;

  timeline.to(cursor, {
    autoAlpha: 1,
    x: pointerPosition(root, target, "x"),
    y: pointerPosition(root, target, "y"),
    duration,
    ease: "power2.inOut",
  });
}

function clickPointer(timeline: gsap.core.Timeline, root: HTMLElement, target: string) {
  const cursor = root.querySelector<HTMLElement>("[data-walkthrough-cursor]");
  const button = root.querySelector<HTMLElement>(target);
  timeline.to({}, { duration: POINTER_SETTLE_DURATION });
  if (cursor) timeline.to(cursor, { scale: 0.86, duration: 0.1, yoyo: true, repeat: 1 });
  if (button) timeline.to(button, { scale: 0.97, duration: 0.1, yoyo: true, repeat: 1 }, "<");
}

function transitionTo(timeline: gsap.core.Timeline, root: HTMLElement, nextIndex: number) {
  const screens = root.querySelectorAll<HTMLElement>("[data-walkthrough-screen]");
  const cursor = root.querySelector<HTMLElement>("[data-walkthrough-cursor]");
  const current = screens[nextIndex - 1];
  const next = screens[nextIndex];
  const progress = root.querySelector<HTMLElement>(`[data-walkthrough-progress="${nextIndex - 1}"]`);
  const previousIndicator = root.querySelector<HTMLElement>(`[data-walkthrough-indicator="${nextIndex - 1}"]`);
  const indicator = root.querySelector<HTMLElement>(`[data-walkthrough-indicator="${nextIndex}"]`);
  const applicationsNav = root.querySelector<HTMLElement>('[data-demo-nav="Applications"]');
  const requestsNav = root.querySelector<HTMLElement>('[data-demo-nav="Requests"]');

  if (cursor) timeline.to(cursor, { autoAlpha: 0, duration: 0.18 });
  if (progress) timeline.to(progress, { scaleX: 1, duration: 0.24, ease: "power2.out" }, "<");
  if (current) timeline.to(current, { autoAlpha: 0, y: -10, duration: 0.32, ease: "power2.in" }, "<");
  if (next) timeline.to(next, { autoAlpha: 1, y: 0, duration: 0.4, ease: "power2.out" }, "<0.16");
  if (previousIndicator) timeline.to(previousIndicator, { color: "#748196", duration: 0.2 }, "<");
  if (indicator) timeline.to(indicator, { color: "#172334", duration: 0.2 }, "<");
  if (nextIndex === 3 && applicationsNav && requestsNav) {
    timeline.call(() => {
      applicationsNav.classList.remove(styles.navItemActive);
      requestsNav.classList.add(styles.navItemActive);
    }, [], "<");
  }
}

export function LandingProductWalkthrough() {
  const rootRef = useRef<HTMLElement>(null);

  useGSAP(() => {
    const root = rootRef.current;
    if (!root) return;

    const screens = Array.from(root.querySelectorAll<HTMLElement>("[data-walkthrough-screen]"));
    const cursor = root.querySelector<HTMLElement>("[data-walkthrough-cursor]");
    const progressBars = root.querySelectorAll<HTMLElement>("[data-walkthrough-progress]");
    const indicators = root.querySelectorAll<HTMLElement>("[data-walkthrough-indicator]");
    if (screens.length !== 4 || !cursor) return;

    const media = gsap.matchMedia();
    media.add("(prefers-reduced-motion: no-preference)", () => {
      const timeline = gsap.timeline({ repeat: -1, repeatDelay: 0.7, repeatRefresh: true });
      const nameValue = root.querySelector("[data-demo-name-value]");
      const keyNameValue = root.querySelector("[data-demo-key-name-value]");
      const promptValue = root.querySelector("[data-demo-prompt-value]");
      const settingsContent = root.querySelector("[data-demo-settings-content]");
      const createdToast = root.querySelector("[data-demo-created]");
      const keyResult = root.querySelector("[data-demo-key-result]");
      const keyEmpty = root.querySelector("[data-demo-key-empty]");
      const overviewScene = root.querySelector("[data-demo-app-overview]");
      const settingsScene = root.querySelector("[data-demo-settings-scene]");
      const responseEmpty = root.querySelector("[data-demo-response-empty]");
      const response = root.querySelector("[data-demo-response]");
      const responseHeading = root.querySelector<HTMLElement>("[data-demo-response-heading]");
      const responseStatus = root.querySelector<HTMLElement>("[data-demo-response-status]");
      const running = root.querySelector("[data-demo-running]");
      const requestList = root.querySelector("[data-demo-request-list]");
      const requestDetail = root.querySelector("[data-demo-request-detail]");

      gsap.set(screens, { autoAlpha: 0, y: 12 });
      gsap.set(screens[0], { autoAlpha: 1, y: 0 });
      gsap.set(cursor, { autoAlpha: 0, x: 0, y: 0, xPercent: -50, yPercent: -50 });
      gsap.set(progressBars, { scaleX: 0, transformOrigin: "left center" });
      gsap.set(indicators, { color: "#748196" });
      gsap.set(indicators[0], { color: "#172334" });
      gsap.set([nameValue, keyNameValue, promptValue], { clipPath: "inset(0 100% 0 0)" });
      gsap.set(createdToast, { autoAlpha: 0, y: 8 });
      gsap.set(settingsContent, { y: 0 });
      gsap.set(overviewScene, { autoAlpha: 1 });
      gsap.set(settingsScene, { autoAlpha: 0 });
      gsap.set(keyResult, { autoAlpha: 0, y: 8, display: "none" });
      gsap.set(keyEmpty, { autoAlpha: 1, display: "flex" });
      gsap.set([running, requestDetail], { autoAlpha: 0 });
      gsap.set(responseStatus, { autoAlpha: 0 });
      gsap.set(responseEmpty, { autoAlpha: 1 });
      gsap.set(response, { autoAlpha: 0, y: 10 });
      gsap.set(requestList, { autoAlpha: 1 });

      timeline.to({}, { duration: 0.28 });
      movePointer(timeline, root, "[data-demo-name]");
      timeline.to(nameValue, { clipPath: "inset(0 0% 0 0)", duration: 0.82, ease: "steps(23)" }, "-=0.12");
      movePointer(timeline, root, "[data-demo-create]");
      clickPointer(timeline, root, "[data-demo-create]");
      timeline.to(createdToast, { autoAlpha: 1, y: 0, duration: 0.32, ease: "power2.out" });
      timeline.to(progressBars[0], { scaleX: 1, duration: 0.28, ease: "power2.out" }, "-=0.1");
      timeline.to({}, { duration: 0.36 });

      transitionTo(timeline, root, 1);
      movePointer(timeline, root, "[data-demo-settings-tab]");
      clickPointer(timeline, root, "[data-demo-settings-tab]");
      timeline.to(overviewScene, { autoAlpha: 0, duration: 0.22, ease: "power1.in" });
      timeline.to(settingsScene, { autoAlpha: 1, duration: 0.34, ease: "power1.out" }, "<0.1");
      timeline.to(settingsContent, { y: -130, duration: 0.62, ease: "power2.inOut" });
      movePointer(timeline, root, "[data-demo-key-name]");
      timeline.to(keyNameValue, { clipPath: "inset(0 0% 0 0)", duration: 0.56, ease: "steps(14)" }, "-=0.12");
      movePointer(timeline, root, "[data-demo-generate]");
      clickPointer(timeline, root, "[data-demo-generate]");
      timeline.to(keyEmpty, { autoAlpha: 0, duration: 0.16 }, "-=0.08");
      timeline.set(keyEmpty, { display: "none" });
      timeline.set(keyResult, { display: "grid" });
      timeline.to(keyResult, { autoAlpha: 1, y: 0, duration: 0.38, ease: "power2.out" });
      timeline.to(progressBars[1], { scaleX: 1, duration: 0.26, ease: "power2.out" }, "-=0.08");
      timeline.to({}, { duration: 0.4 });

      transitionTo(timeline, root, 2);
      movePointer(timeline, root, "[data-demo-prompt]");
      timeline.to(promptValue, { clipPath: "inset(0 0% 0 0)", duration: 0.92, ease: "steps(30)" }, "-=0.1");
      movePointer(timeline, root, "[data-demo-run]");
      clickPointer(timeline, root, "[data-demo-run]");
      timeline.to(running, { autoAlpha: 1, duration: 0.2 });
      timeline.to(responseEmpty, { autoAlpha: 0, duration: 0.2 }, "<");
      timeline.to(response, { autoAlpha: 1, y: 0, duration: 0.48, ease: "power2.out" }, "+=0.55");
      timeline.to(running, { autoAlpha: 0, duration: 0.16 }, "<");
      timeline.to(responseStatus, { autoAlpha: 1, duration: 0.2 }, "<");
      timeline.call(() => { if (responseHeading) responseHeading.textContent = "Saved routing result"; }, [], "<");
      timeline.to(progressBars[2], { scaleX: 1, duration: 0.25, ease: "power2.out" }, "-=0.08");
      timeline.to({}, { duration: 0.38 });

      transitionTo(timeline, root, 3);
      movePointer(timeline, root, '[data-demo-nav="Requests"]');
      clickPointer(timeline, root, '[data-demo-nav="Requests"]');
      movePointer(timeline, root, "[data-demo-request-row]");
      clickPointer(timeline, root, "[data-demo-request-row]");
      timeline.to(requestList, { autoAlpha: 0, y: -8, duration: 0.24, ease: "power2.in" });
      timeline.to(requestDetail, { autoAlpha: 1, y: 0, duration: 0.42, ease: "power2.out" }, "<0.12");
      const traceNodes = root.querySelectorAll<HTMLElement>("[data-demo-trace-node]");
      traceNodes.forEach((node, index) => {
        timeline.to(node, { autoAlpha: 1, y: 0, duration: 0.24, ease: "power2.out" }, index === 0 ? "+=0.08" : "-=0.02");
      });
      timeline.to(progressBars[3], { scaleX: 1, duration: 0.28, ease: "power2.out" }, "-=0.08");
      timeline.to({}, { duration: 0.78 });

      timeline.to(cursor, { autoAlpha: 0, duration: 0.18 });
      timeline.to(screens[3], { autoAlpha: 0, y: -10, duration: 0.32, ease: "power2.in" }, "<");
      timeline.set(screens, { autoAlpha: 0, y: 12 });
      timeline.set(screens[0], { autoAlpha: 1, y: 0 });
      timeline.set(progressBars, { scaleX: 0 });
      timeline.set(indicators, { color: "#748196" });
      timeline.set(indicators[0], { color: "#172334" });
      timeline.set(createdToast, { autoAlpha: 0, y: 8 });
      timeline.set(settingsContent, { y: 0 });
      timeline.set(overviewScene, { autoAlpha: 1 });
      timeline.set(settingsScene, { autoAlpha: 0 });
      timeline.set(keyResult, { display: "none", autoAlpha: 0, y: 8 });
      timeline.set(keyEmpty, { display: "flex", autoAlpha: 1 });
      timeline.set(running, { autoAlpha: 0 });
      timeline.set(responseStatus, { autoAlpha: 0 });
      timeline.call(() => { if (responseHeading) responseHeading.textContent = "Ready for a test"; });
      timeline.set(responseEmpty, { autoAlpha: 1 });
      timeline.set(response, { autoAlpha: 0, y: 10 });
      timeline.set(requestList, { autoAlpha: 1, y: 0 });
      timeline.set(requestDetail, { autoAlpha: 0, y: 10 });
      const applicationsNav = root.querySelector<HTMLElement>('[data-demo-nav="Applications"]');
      const requestsNav = root.querySelector<HTMLElement>('[data-demo-nav="Requests"]');
      if (applicationsNav && requestsNav) timeline.call(() => {
        applicationsNav.classList.add(styles.navItemActive);
        requestsNav.classList.remove(styles.navItemActive);
      });
      timeline.set(traceNodes, { autoAlpha: 0, y: 8 });

      const observer = new IntersectionObserver(([entry]) => {
        if (entry.isIntersecting) timeline.play();
        else timeline.pause();
      }, { threshold: 0.12 });
      observer.observe(root);

      return () => {
        observer.disconnect();
        timeline.kill();
      };
    }, root);

    return () => media.revert();
  }, { scope: rootRef });

  return (
    <section className={styles.walkthrough} data-motion="hero-visual" ref={rootRef} aria-label="Taskway product walkthrough">
      <p className={styles.srOnly}>Product walkthrough: create an application, generate an API key in its settings, run a Playground request, and inspect its request trace, model, and cost.</p>
      <div className={styles.workspaceFrame} aria-hidden="true">
        <div className={styles.workspace} data-walkthrough-canvas>
          <aside className={styles.sidebar}>
            <div className={styles.sidebarHeader}>
              <span className={styles.workspaceLogo}><TaskwayLogo appearance="theme-aware" className="taskway-logo--workspace" /></span>
              <span className={styles.collapseIcon}><i /><i /><i /></span>
            </div>
            <div className={styles.workspaceLabel}><small>WORKSPACE ACTIVE</small><strong>User</strong></div>
            <div className={styles.navGroup}>
              <p>WORKSPACE</p>
              <div className={styles.navItem}><span className={styles.navIcon}>▦</span>Overview</div>
              <div className={styles.navItem}><span className={styles.navIcon}>⌘</span>SDK Guide</div>
              <div className={`${styles.navItem} ${styles.navItemActive}`} data-demo-nav="Applications"><span className={styles.navIcon}>▣</span>Applications</div>
            </div>
            <div className={styles.navGroup}>
              <p>OPERATE</p>
              <div className={styles.navItem}><span className={styles.navIcon}>◈</span>Models</div>
              <div className={styles.navItem}><span className={styles.navIcon}>⌁</span>Providers</div>
              <div className={styles.navItem}><span className={styles.navIcon}>◇</span>Model pools</div>
              <div className={styles.navItem} data-demo-nav="Requests"><span className={styles.navIcon}>↗</span>Requests</div>
              <div className={styles.navItem}><span className={styles.navIcon}>▥</span>Usage</div>
            </div>
          </aside>

          <main className={styles.productMain}>
            <header className={styles.topbar}>
              <span className={styles.topbarSpacer} />
              <div className={styles.topbarActions}><span>☼</span><span>EN</span><b>U</b></div>
            </header>
            <div className={styles.screenHost}>
              <div className={styles.screen} data-walkthrough-screen>
                <div className={styles.pageHeading}><div><small>APPLICATIONS</small><h3>Connect your first application</h3><p>Register a workload to start routing requests.</p></div></div>
                <div className={styles.newApplicationLayout}>
                  <div className={styles.applicationForm}>
                    <div className={styles.panelHeader}><span className={styles.panelIcon}>▣</span><div><small>CONFIGURATION</small><strong>Application details</strong></div><i>REQUIRED</i></div>
                    <div className={styles.formFields}>
                      <div className={styles.field}><label>Name <b>*</b></label><div className={styles.input} data-demo-name><span data-demo-name-value>Customer Support Bot</span><i className={styles.caret} /></div></div>
                      <div className={styles.field}><label>Slug</label><div className={styles.inputMuted}>customer-support-bot</div></div>
                      <div className={styles.fieldGrid}>
                        <div className={styles.field}><label>Environment</label><div className={styles.selectValue}>Development <span>⌄</span></div></div>
                        <div className={styles.field}><label>Monthly budget (USD)</label><div className={styles.inputMuted}>250.00</div></div>
                      </div>
                    </div>
                    <div className={styles.formFooter}><span>Change routing and budget later.</span><div className={styles.primaryButton} data-demo-create>Create application <b>→</b></div></div>
                  </div>
                  <aside className={styles.applicationPreview}>
                    <div className={styles.previewHeading}><div><small>LIVE PREVIEW</small><strong>Your application</strong></div><span>DRAFT</span></div>
                    <div className={styles.previewIdentity}><i>A</i><div><strong>Customer Support Bot</strong><small>customer-support-bot</small></div></div>
                    <dl><div><dt>Environment</dt><dd><i /> Development</dd></div><div><dt>Monthly budget</dt><dd>$250.00</dd></div></dl>
                    <div className={styles.previewNext}><strong>What happens next?</strong><small>Generate a key, then send your first request.</small></div>
                  </aside>
                </div>
                <div className={styles.createdToast} data-demo-created><span>✓</span> Application created <small>Opening application…</small></div>
              </div>

              <div className={`${styles.screen} ${styles.applicationScreen}`} data-walkthrough-screen>
                <div className={styles.applicationOverview} data-demo-app-overview>
                  <div className={styles.pageHeading}><div><small>DEVELOPMENT APPLICATION</small><h3>Customer Support Bot</h3><p>customer-support-bot · Configuration version 3</p></div><span className={styles.stepCount}>02 / 04</span></div>
                  <div className={styles.applicationTabs}><span className={styles.tabActive}>Overview</span><span>Playground</span><span data-demo-settings-tab>Settings</span></div>
                  <div className={styles.overviewMetrics}><article><small>ROUTING OBJECTIVE</small><strong>Balanced</strong><span>Server configuration</span></article><article><small>AVAILABLE MODELS</small><strong>12 enabled</strong><span>Active model catalog</span></article><article><small>BUDGET GUARDRAIL</small><strong>$250.00</strong><span>Monthly ceiling</span></article><article><small>REQUESTS</small><strong>0</strong><span>Owned by this application</span></article></div>
                  <div className={styles.recentRequests}><div><strong>Recent requests</strong><span>View all requests →</span></div><p>No recent requests yet. Send your first request in Playground.</p></div>
                </div>
                <div className={styles.applicationSettings} data-demo-settings-scene>
                  <div className={styles.pageHeading}><div><small>APPLICATION CONFIGURATION · V3</small><h3>Settings</h3><p>Manage routing behavior, budget, and API credentials.</p></div><span className={styles.stepCount}>02 / 04</span></div>
                  <div className={styles.applicationTabs}><span>Overview</span><span>Playground</span><span className={styles.tabActive}>Settings</span></div>
                  <div className={styles.settingsViewport}>
                    <div className={styles.settingsContent} data-demo-settings-content>
                      <div className={styles.settingsCard}>
                        <div className={styles.settingsCardHeading}><div><small>CONFIGURATION · V3</small><strong>Routing settings</strong></div></div>
                        <div className={styles.settingsFields}><div className={styles.field}><label>Routing objective</label><div className={styles.selectValue}>Balanced <span>⌄</span></div></div><div className={styles.field}><label>Monthly budget</label><div className={styles.inputMuted}>$250.00</div></div><div className={styles.field}><label>Maximum model latency (ms)</label><div className={styles.inputMuted}>No limit</div></div><div className={styles.field}><label>Timeout seconds</label><div className={styles.inputMuted}>30</div></div><div className={styles.field}><label>Retry limit</label><div className={styles.inputMuted}>2</div></div></div>
                        <div className={styles.formFooter}><span>Version 3</span><div className={styles.secondaryButton}>Save changes</div></div>
                      </div>
                      <div className={styles.keyCard}>
                        <div className={styles.keyHeading}><div><small>API CREDENTIALS</small><strong>API keys</strong><p>Keys give this application access to the gateway.</p></div><span>＋</span></div>
                        <div className={styles.keyForm}><div className={styles.input} data-demo-key-name><span data-demo-key-name-value>Production key</span><i className={styles.caret} /></div><div className={styles.primaryButtonSmall} data-demo-generate>Create key</div></div>
                        <div className={styles.keyEmpty} data-demo-key-empty>No API keys yet</div>
                        <div className={styles.keyResult} data-demo-key-result>
                          <div className={styles.keyNotice}><strong>Copy your key now</strong><small>This secret is shown once. Store it securely.</small><code>tw_live_••••••••••••</code><span>Copy key</span></div>
                          <div className={styles.keyTable}><b>Name</b><b>Prefix</b><b>Status</b><span>Production key</span><code>tw_live…</code><i>Active</i></div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <div className={styles.screen} data-walkthrough-screen>
                <div className={styles.pageHeading}><div><small>CUSTOMER SUPPORT BOT · PLAYGROUND</small><h3>Test the real routing path.</h3><p>Requests are executed and saved by the Backend AI runtime.</p></div><span className={styles.stepCount}>03 / 04</span></div>
                <div className={styles.applicationTabs}><span>Overview</span><span className={styles.tabActive}>Playground</span><span>Settings</span></div>
                <div className={styles.playgroundGrid}>
                  <div className={styles.composerCard}>
                    <div className={styles.cardTitle}><div><small>PROMPT</small><strong>Request composer</strong></div><span className={styles.liveStatus}><i /> Live · billable</span></div>
                    <div className={styles.field}><label>Prompt <b>*</b></label><div className={styles.promptInput} data-demo-prompt><span data-demo-prompt-value>Summarize the latest customer issue and suggest a helpful reply.</span><i className={styles.caret} /></div><small className={styles.tokenHint}>14 estimated tokens</small></div>
                    <div className={styles.formFooter}><span>Clear</span><div className={styles.primaryButton} data-demo-run><i>▶</i> Run routing</div></div>
                    <div className={styles.composerMetrics}><span><small>MODEL</small><b>GPT-4.1</b></span><span><small>TOKENS</small><b>348</b></span><span><small>COST</small><b>$0.0024</b></span></div>
                  </div>
                  <div className={styles.responseCard}>
                    <div className={styles.cardTitle}><div><small>ROUTING RESPONSE</small><strong data-demo-response-heading>Ready for a test</strong></div><span className={styles.responseStatus} data-demo-response-status>Completed</span></div>
                    <div className={styles.responseEmpty} data-demo-response-empty>The persisted response, routing decision, and real cost will appear here.</div>
                    <div className={styles.responseBody} data-demo-response>
                      <p>The customer is asking for an update on their delayed order. Acknowledge the delay, share the latest delivery estimate, and offer to help if it does not arrive.</p>
                      <div className={styles.resultMeta}><span><small>SELECTED MODEL</small><b>GPT-4.1 · OpenAI</b></span><span><small>ESTIMATED COST</small><b>$0.0024</b></span></div>
                    </div>
                    <div className={styles.runningStatus} data-demo-running><i /> Running Backend AI pipeline</div>
                  </div>
                </div>
              </div>

              <div className={styles.screen} data-walkthrough-screen>
                <div className={styles.pageHeading}><div><small>OPERATE · REQUESTS</small><h3>Request history</h3><p>Search completed and failed requests across your workspace.</p></div><span className={styles.stepCount}>04 / 04</span></div>
                <div className={styles.requestList} data-demo-request-list>
                  <div className={styles.requestFilters}><span>⌕ <i>Filter requests</i></span><b>＋ Add filter</b></div>
                  <div className={styles.requestTable}>
                    <div className={styles.tableHead}><span>REQUEST</span><span>APPLICATION</span><span>STATUS</span><span>COST</span></div>
                    <div className={styles.requestRow} data-demo-request-row><span><b>req_01JQ8F…</b><small>Oct 15 · 10:42 AM</small></span><span><b>Customer Support Bot</b><small>app_7f13…</small></span><span className={styles.completed}><i /> Completed</span><strong>$0.0024</strong></div>
                  </div>
                </div>
                <div className={styles.requestDetail} data-demo-request-detail>
                  <div className={styles.detailHeading}><div><small>REQUEST TRACE · req_01JQ8F…</small><strong>Request trace</strong><span>Customer Support Bot / playground</span></div><span className={styles.completed}><i /> Completed</span></div>
                  <div className={styles.requestFacts}><span><small>LATENCY</small><b>682 ms</b></span><span><small>ESTIMATED COST</small><b>$0.0024</b></span><span><small>MODELS USED</small><b>GPT-4.1</b></span><span><small>FALLBACK</small><b>Not used</b></span></div>
                  <div className={styles.routingMap}>
                    <div className={styles.routingLane}><small>01 / INTAKE</small><div data-demo-trace-node><i className={styles.intakeMark}>P</i><span><b>Playground</b><em>Prompt accepted</em><code>req_01JQ8F…</code></span></div></div>
                    <span className={styles.mapConnector}>→</span>
                    <div className={styles.routingLane}><small>02 / ROUTING CORE</small><div data-demo-trace-node><i className={styles.gatewayMark}>G</i><span><b>Gateway policy</b><em>Primary route selected</em><code>Policy matched</code></span></div></div>
                    <span className={styles.mapConnector}>→</span>
                    <div className={styles.routingLane}><small>03 / MODEL</small><div data-demo-trace-node><i className={styles.modelMark}>M</i><span><b>GPT-4.1</b><em>OpenAI / completed</em><code>348 tokens · 682 ms</code></span></div></div>
                    <span className={styles.mapConnector}>→</span>
                    <div className={styles.routingLane}><small>04 / AGGREGATION</small><div data-demo-trace-node><i className={styles.aggregateMark}>R</i><span><b>Response</b><em>Result saved</em><code>$0.0024</code></span></div></div>
                  </div>
                </div>
              </div>

              <div className={styles.cursor} data-walkthrough-cursor><svg viewBox="0 0 22 26"><path d="M3 2.6c0-1.2 1.4-1.8 2.3-.9l14.1 14.1c.9.9.3 2.4-1 2.4l-5.5.1 3 5.8c.3.6.1 1.3-.5 1.6l-1.8.9c-.6.3-1.3.1-1.6-.5L11 20.1l-3.5 4.2c-.8 1-2.3.5-2.4-.8L3 2.6Z" fill="#172334" stroke="#fff" strokeWidth="1.4" /></svg></div>
            </div>
          </main>
        </div>
        <footer className={styles.timelineFooter} aria-hidden="true">
          {steps.map((step, index) => (
            <div className={styles.timelineStep} key={step}>
              <div className={styles.timelineStepTop}><span data-walkthrough-indicator={index}>{step}</span><b>0{index + 1}</b></div>
              <div className={styles.progressTrack}><i data-walkthrough-progress={index} /></div>
            </div>
          ))}
        </footer>
      </div>
    </section>
  );
}
