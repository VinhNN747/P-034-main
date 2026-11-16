"use client";

import { useEffect, useId, useRef, useState } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";

import { SupportChatReferences } from "@/components/support-chat-references";
import { SupportMessageContent } from "@/components/support-message-content";
import { TaskwayAssistantMark } from "@/components/taskway-assistant-mark";
import { useLanguage } from "@/components/language-provider";
import { LandingApplicationLoading, LandingApplicationPreview } from "@/components/landing-application-preview";
import styles from "./landing-faq-walkthrough.module.css";

type Source = { id: string; title: string; url: string };
type Action = { id?: string; label: string; url: string; onActivate?: () => void };
type ChatTurn = {
  question: string;
  answer: string;
  source: Source;
  actions: Action[];
  code?: string;
  chart?: boolean;
};
type ChatScene = { key: string; label: string; turns: ChatTurn[] };

const REQUEST_VALUES = [42, 74, 96, 48, 37, 41, 57, 63];
const CHART_HOVER_POINT_INDICES = [2, 5] as const;
const APPLICATION_LOADING_DURATION = 0.72;
const APPLICATION_PREVIEW_DURATION = 1.4;
const POINTER_TRAVEL_DURATION = 0.68;
const POINTER_PRESS_DURATION = 0.1;
const COPY_BUTTON_HOVER_DURATION = 0.28;
const CHART_POINTER_TRAVEL_DURATION = 0.42;
const CHART_POINT_HOVER_DURATION = 0.48;
const CHART_BASELINE_Y = 116;
const CHART_REVEAL_TOP_Y = 36;
const CHART_REVEAL_HEIGHT = CHART_BASELINE_Y - CHART_REVEAL_TOP_Y;
const CHART_DRAW_DURATION = 1.6;
const CHART_POINT_RISE_DURATION = 0.16;
const SCENE_START_DELAY = 0.18;
const DEMO_APPLICATIONS = [
  { slug: "faq-assistant", name: "FAQ Assistant", status: "active", environment: "production" },
  { slug: "customer-support-portal", name: "Customer Support Portal", status: "active", environment: "development" },
  { slug: "order-tracking-bot", name: "Order Tracking Bot", status: "active", environment: "development" },
  { slug: "returns-refunds-helper", name: "Returns & Refunds Helper", status: "active", environment: "development" },
  { slug: "product-guide", name: "Product Guide", status: "active", environment: "development" },
  { slug: "knowledge-base-search", name: "Knowledge Base Search", status: "active", environment: "development" },
  { slug: "sales-lead-assistant", name: "Sales Lead Assistant", status: "active", environment: "development" },
] as const;

function demoApplicationsAnswer(intro: string) {
  return `${intro}\n${DEMO_APPLICATIONS.map(({ name, status, environment }) => `${name} (${status}, ${environment})`).join("\n")}`;
}

function chartPoints(values: number[]) {
  const minimum = Math.min(...values);
  const range = Math.max(...values) - minimum || 1;
  return values.map((value, index) => ({
    x: 12 + (336 * index) / (values.length - 1),
    y: 108 - ((value - minimum) / range) * 72,
  }));
}

function LandingRequestChart({ label }: { label: string }) {
  const clipId = `request-chart-reveal-${useId().replace(/:/g, "")}`;
  const points = chartPoints(REQUEST_VALUES);
  const linePath = points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(" ");
  const areaPath = `${linePath} L 348 116 L 12 116 Z`;

  return (
    <figure className={styles.chartCard} data-faq-chart>
      <figcaption className={styles.chartHeading}>
        <span><b>{label}</b><small>requests · day</small></span>
        <span className={styles.chartType}><i /> line</span>
      </figcaption>
      <svg className={styles.chart} viewBox="0 0 360 126" role="img" aria-label={label}>
        <defs>
          <clipPath id={clipId} clipPathUnits="userSpaceOnUse">
            <rect data-faq-chart-reveal x="12" y={CHART_BASELINE_Y} width="336" height="0" />
          </clipPath>
        </defs>
        <path className={styles.chartGrid} d="M12 36H348M12 62H348M12 88H348M12 114H348" />
        <path className={styles.chartArea} data-faq-chart-area d={areaPath} clipPath={`url(#${clipId})`} />
        <path className={styles.chartLine} data-faq-chart-line d={linePath} pathLength={1} />
        {points.map((point, index) => (
          <circle
            className={styles.chartPoint}
            data-faq-chart-point
            data-faq-chart-index={index}
            data-faq-chart-target-y={point.y}
            key={`${point.x}-${index}`}
            cx={point.x}
            cy={point.y}
            r="3.2"
          />
        ))}
        {CHART_HOVER_POINT_INDICES.map((index) => {
          const point = points[index];
          const day = index === points.length - 1 ? "Today" : `D-${points.length - 1 - index}`;
          return (
            <g className={styles.chartTooltip} data-faq-chart-tooltip={index} key={`tooltip-${index}`} aria-hidden="true">
              <rect className={styles.chartTooltipBox} x={point.x - 35} y={point.y - 29} width="70" height="23" rx="6" fill="#fff" />
              <text className={styles.chartTooltipDay} x={point.x} y={point.y - 19} fontSize="5.5" fontFamily="Arial, sans-serif" textAnchor="middle" fill="#74819b">{day}</text>
              <text className={styles.chartTooltipValue} x={point.x} y={point.y - 9} fontSize="6.2" fontFamily="Arial, sans-serif" fontWeight="700" textAnchor="middle" fill="#344054">{REQUEST_VALUES[index]} requests</text>
            </g>
          );
        })}
      </svg>
      <div className={styles.chartTicks} aria-hidden="true">
        <span>D-7</span><span>D-6</span><span>D-5</span><span>D-4</span>
        <span>D-3</span><span>D-2</span><span>D-1</span><span>Today</span>
      </div>
    </figure>
  );
}

export function LandingFaqWalkthrough({ className }: { className: string }) {
  const { language, t } = useLanguage();
  const rootRef = useRef<HTMLDivElement>(null);
  const timelineRef = useRef<gsap.core.Timeline | null>(null);
  const reducedMotionRef = useRef(false);
  const automaticActivationRef = useRef(false);
  const automaticPreviewRef = useRef(false);
  const skipAutomaticPreviewRef = useRef(false);
  const locationRef = useRef({ scene: 0, frame: 0 });
  const [activeLocation, setActiveLocation] = useState({ scene: 0, frame: 0 });
  const [previewMode, setPreviewMode] = useState<"chat" | "loading" | "application">("chat");
  const isVietnamese = language === "vi";

  useEffect(() => {
    if (previewMode !== "loading") return;
    const timer = window.setTimeout(() => setPreviewMode("application"), APPLICATION_LOADING_DURATION * 1000);
    return () => window.clearTimeout(timer);
  }, [previewMode]);

  const copy = isVietnamese
    ? {
        ready: "Trợ lý đang hoạt động",
        apps: "Applications",
        setup: "Khởi tạo",
        usage: "Usage",
        promptApplications: "Liệt kê application của tôi",
        answerApplications: demoApplicationsAnswer("Workspace có 7 application mẫu. Gần đây:"),
        sourceApplications: "Applications của bạn",
        actionApplications: "Mở Applications",
        actionOpenApplication: (name: string) => `Mở ${name}`,
        promptSetup: "Tôi muốn tạo application mới cho chatbot FAQ. Gợi ý cấu hình khởi đầu nhé.",
        answerSetup: "1. **Tạo application:** vào Applications → New application; tên FAQ Chatbot Dev, slug `faq-chatbot-dev`, chọn môi trường development.\n2. **Cấu hình Gateway:** chọn một model khả dụng hoặc pool đã cấu hình, dùng routing đơn giản; đặt budget/output limit phù hợp, chỉ bật strategy cần thiết và thêm fallback khi đã chọn model dự phòng.\n3. **Chạy thử & theo dõi:** xác nhận application active, thử câu hỏi FAQ trong Playground; theo dõi token, chi phí và độ trễ trong Usage.\n4. **Chuẩn bị tích hợp:** tạo API key riêng và sao chép ngay (key đầy đủ chỉ hiện một lần). Nếu thiếu model, nhờ admin kiểm tra model pool.",
        sourceSetup: "Tạo application",
        actionCreate: "Tạo application",
        actionModels: "Xem model khả dụng",
        actionUsage: "Xem Usage",
        promptCode: "Cho tôi code Python để gọi model qua Gateway.",
        answerCode: "Cài SDK, giữ key trong biến môi trường và dùng model auto.",
        codeResponse: `\`\`\`python
from uuid import uuid4
from openai import OpenAI
import os

client = OpenAI(api_key=os.getenv("TASKWAY_API_KEY"),
                base_url="https://taskway-ai-be.online/v1")
response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Xin chào!"}],
    extra_headers={"Idempotency-Key": str(uuid4())},
)
\`\`\`
`,
        sourceSdk: "Hướng dẫn dùng SDK Taskway",
        actionSdk: "Mở SDK Guide",
        promptUsage: "Hãy vẽ biểu đồ số lượng request theo ngày trong 7 ngày gần nhất.",
        answerUsage: "Request theo ngày trong 7 ngày gần nhất.",
        sourceUsage: "Request — các application của bạn",
        chartTitle: "Requests · 7 ngày",
        scene: "Cảnh",
      }
    : {
        ready: "Assistant is online",
        apps: "Applications",
        setup: "Setup",
        usage: "Usage",
        promptApplications: "List my applications",
        answerApplications: demoApplicationsAnswer("This workspace has 7 sample applications. Recently used:"),
        sourceApplications: "Your applications",
        actionApplications: "Open Applications",
        actionOpenApplication: (name: string) => `Open ${name}`,
        promptSetup: "I’m creating an application for a website FAQ chatbot. What setup do you recommend?",
        answerSetup: "1. **Create the application:** go to Applications → New application; use FAQ Chatbot Dev, slug `faq-chatbot-dev`, and the development environment.\n2. **Configure Gateway:** choose an available model or configured pool and simple routing; set a suitable budget/output limit, enable only needed strategies, and add fallback after choosing a backup model.\n3. **Test & monitor:** confirm it is active, try FAQ questions in Playground, then track tokens, cost, and latency in Usage.\n4. **Prepare integration:** create a dedicated API key and copy it immediately (the full key appears once). If no model is available, ask an admin to check the model pool.",
        sourceSetup: "Create an application",
        actionCreate: "Create application",
        actionModels: "View available models",
        actionUsage: "View Usage",
        promptCode: "Show me Python code to call a model through the Gateway.",
        answerCode: "Install the SDK, keep the key in an environment variable, and use model auto.",
        codeResponse: `\`\`\`python
from uuid import uuid4
from openai import OpenAI
import os

client = OpenAI(api_key=os.getenv("TASKWAY_API_KEY"),
                base_url="https://taskway-ai-be.online/v1")
response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Hello!"}],
    extra_headers={"Idempotency-Key": str(uuid4())},
)
\`\`\`
`,
        sourceSdk: "Taskway SDK guide",
        actionSdk: "Open SDK Guide",
        promptUsage: "Draw a chart of daily request volume for the last seven days.",
        answerUsage: "Requests by day over the last seven days.",
        sourceUsage: "Requests — your applications",
        chartTitle: "Requests · 7 days",
        scene: "Scene",
      };

  const codeSource: Source = {
    id: "sdk-guide",
    title: copy.sourceSdk,
    url: "https://minminhmin.github.io/taskway.github.io/vi/docs/quickstart/#install-the-sdk",
  };

  function openApplicationPreview() {
    setPreviewMode("loading");
    if (automaticActivationRef.current) {
      automaticPreviewRef.current = true;
      return;
    }
    skipAutomaticPreviewRef.current = true;
    const pointer = rootRef.current?.querySelector<HTMLElement>("[data-faq-pointer]");
    if (pointer) gsap.set(pointer, { autoAlpha: 0 });
    timelineRef.current?.pause();
  }

  function returnToChat() {
    automaticPreviewRef.current = false;
    setPreviewMode("chat");
    if (!reducedMotionRef.current) timelineRef.current?.play();
  }

  function clearCopyButtonHover() {
    rootRef.current?.querySelector<HTMLElement>("[data-faq-demo-copy]")?.removeAttribute("data-faq-demo-hover");
  }

  function clearChartHover() {
    const root = rootRef.current;
    if (!root) return;
    gsap.set(root.querySelectorAll("[data-faq-chart-tooltip]"), { autoAlpha: 0 });
    gsap.set(root.querySelectorAll("[data-faq-chart-reveal]"), {
      attr: {
        y: reducedMotionRef.current ? CHART_REVEAL_TOP_Y : CHART_BASELINE_Y,
        height: reducedMotionRef.current ? CHART_REVEAL_HEIGHT : 0,
      },
    });
    gsap.set(root.querySelectorAll("[data-faq-chart-area]"), { autoAlpha: reducedMotionRef.current ? 1 : 0 });
    gsap.set(root.querySelectorAll("[data-faq-chart-line]"), { strokeDashoffset: reducedMotionRef.current ? 0 : 1 });
    root.querySelectorAll<SVGCircleElement>("[data-faq-chart-point]").forEach((point) => {
      const targetY = Number(point.dataset.faqChartTargetY);
      gsap.set(point, {
        attr: {
          cy: reducedMotionRef.current ? targetY : CHART_BASELINE_Y,
          r: reducedMotionRef.current ? 3.2 : 0,
        },
        clearProps: "fill",
      });
    });
  }

  const scenes: ChatScene[] = [
    {
      key: "applications",
      label: copy.apps,
      turns: [{
        question: copy.promptApplications,
        answer: copy.answerApplications,
        source: { id: "applications", title: copy.sourceApplications, url: "/applications" },
        actions: [
          { label: copy.actionApplications, url: "/applications" },
          ...DEMO_APPLICATIONS.map((application) => ({
            id: application.slug === "faq-assistant" ? "demo-faq-assistant" : `demo-application-${application.slug}`,
            label: application.slug === "faq-assistant" ? "FAQ Assistant" : copy.actionOpenApplication(application.name),
            url: "/applications",
            onActivate: application.slug === "faq-assistant" ? openApplicationPreview : undefined,
          })),
        ],
      }],
    },
    {
      key: "setup",
      label: copy.setup,
      turns: [
        {
          question: copy.promptSetup,
          answer: copy.answerSetup,
          source: { id: "create-application", title: copy.sourceSetup, url: "/applications/new" },
          actions: [
            { label: copy.actionCreate, url: "/applications/new" },
            { label: copy.actionModels, url: "/available-models" },
            { label: copy.actionUsage, url: "/usage" },
          ],
        },
        {
          question: copy.promptCode,
          answer: copy.answerCode,
          code: copy.codeResponse,
          source: codeSource,
          actions: [{ label: copy.actionSdk, url: "/sdk-guide" }],
        },
      ],
    },
    {
      key: "usage",
      label: copy.usage,
      turns: [{
        question: copy.promptUsage,
        answer: copy.answerUsage,
        chart: true,
        source: { id: "request-timeseries", title: copy.sourceUsage, url: "/usage" },
        actions: [{ label: copy.actionUsage, url: "/usage" }],
      }],
    },
  ];

  function updateLocation(scene: number, frame: number) {
    locationRef.current = { scene, frame };
    setActiveLocation({ scene, frame });
  }

  function selectScene(index: number) {
    automaticPreviewRef.current = false;
    skipAutomaticPreviewRef.current = false;
    clearCopyButtonHover();
    clearChartHover();
    setPreviewMode("chat");
    const pointer = rootRef.current?.querySelector<HTMLElement>("[data-faq-pointer]");
    if (pointer) gsap.set(pointer, { autoAlpha: 0 });
    if (reducedMotionRef.current || !timelineRef.current) {
      updateLocation(index, 0);
      return;
    }
    timelineRef.current.play(`scene-${index}`);
  }

  useGSAP(() => {
    const root = rootRef.current;
    if (!root) return;
    const rootElement = root;
    const media = gsap.matchMedia();
    const panels = Array.from(root.querySelectorAll<HTMLElement>("[data-faq-scene]"));
    const frames = Array.from(root.querySelectorAll<HTMLElement>("[data-faq-frame]"));

    media.add("(prefers-reduced-motion: reduce)", () => {
      reducedMotionRef.current = true;
      if (automaticPreviewRef.current) {
        automaticPreviewRef.current = false;
        setPreviewMode("chat");
      }
      clearCopyButtonHover();
      clearChartHover();
      gsap.set(root.querySelectorAll("[data-faq-pointer]"), { autoAlpha: 0 });
      gsap.set(root.querySelectorAll("[data-faq-question]"), { clipPath: "inset(0 0% 0 0)" });
      gsap.set(root.querySelectorAll("[data-faq-typing]"), { autoAlpha: 0, y: 0 });
      gsap.set(root.querySelectorAll("[data-faq-answer], [data-faq-code], [data-faq-references]"), { autoAlpha: 1, y: 0 });
      gsap.set(root.querySelectorAll("[data-faq-chart-area]"), { autoAlpha: 1 });
      gsap.set(root.querySelectorAll("[data-faq-chart-line]"), { strokeDashoffset: 0 });
      gsap.set(root.querySelectorAll("[data-faq-chart-point]"), { attr: { r: 3.2 } });
      automaticActivationRef.current = false;
      return () => { reducedMotionRef.current = false; };
    });

    media.add("(prefers-reduced-motion: no-preference)", () => {
      reducedMotionRef.current = false;
      gsap.set(panels, { autoAlpha: 0, y: 8 });
      gsap.set(frames, { autoAlpha: 0, y: 8 });
      gsap.set(root.querySelectorAll("[data-faq-question]"), { clipPath: "inset(0 100% 0 0)" });
      gsap.set(root.querySelectorAll("[data-faq-typing]"), { autoAlpha: 0, y: 5 });
      gsap.set(root.querySelectorAll("[data-faq-answer], [data-faq-code], [data-faq-references]"), { autoAlpha: 0, y: 8 });
      gsap.set(root.querySelectorAll("[data-faq-chart-area]"), { autoAlpha: 0 });
      gsap.set(root.querySelectorAll("[data-faq-chart-line]"), { strokeDashoffset: 1 });
      gsap.set(root.querySelectorAll("[data-faq-chart-reveal]"), {
        attr: { y: CHART_BASELINE_Y, height: 0 },
      });
      gsap.set(root.querySelectorAll("[data-faq-chart-point]"), {
        attr: { cy: CHART_BASELINE_Y, r: 0 },
      });
      gsap.set(root.querySelectorAll("[data-faq-pointer]"), { autoAlpha: 0, scale: 1 });

      const indicators = Array.from(root.querySelectorAll<HTMLElement>("[data-faq-indicator]"));
      const pointer = root.querySelector<HTMLElement>("[data-faq-pointer]");
      const timeline = gsap.timeline({ paused: true, repeat: -1, repeatDelay: 0.45, defaults: { ease: "power2.out" } });
      timelineRef.current = timeline;
      timeline.eventCallback("onRepeat", () => {
        skipAutomaticPreviewRef.current = false;
        clearCopyButtonHover();
        clearChartHover();
      });

      function measureTarget(selector: string) {
        const target = rootElement.querySelector<HTMLElement>(selector);
        if (!target) return null;
        const rootBounds = rootElement.getBoundingClientRect();
        const targetBounds = target.getBoundingClientRect();
        return {
          x: targetBounds.left - rootBounds.left + targetBounds.width * 0.52,
          y: targetBounds.top - rootBounds.top + targetBounds.height * 0.5,
        };
      }

      const measureFaqTarget = () => measureTarget('[data-chat-action="demo-faq-assistant"]');
      const measureCopyTarget = () => measureTarget("[data-faq-demo-copy]");

      let sceneStart = 0;

      scenes.forEach((scene, sceneIndex) => {
        const panel = panels[sceneIndex];
        const sceneFrames = Array.from(panel.querySelectorAll<HTMLElement>("[data-faq-frame]"));
        timeline.addLabel(`scene-${sceneIndex}`, sceneStart);
        timeline.call(() => updateLocation(sceneIndex, 0), [], sceneStart);
        timeline.set(panel, { autoAlpha: 1, y: 0 }, sceneStart);
        timeline.set(indicators, { scale: 0.96, autoAlpha: 0.58 }, sceneStart);
        timeline.to(indicators[sceneIndex], { scale: 1.04, autoAlpha: 1, duration: 0.28 }, sceneStart);

        let frameStart = sceneStart;
        sceneFrames.forEach((frame, frameIndex) => {
          const question = frame.querySelector<HTMLElement>("[data-faq-question]");
          const typing = frame.querySelector<HTMLElement>("[data-faq-typing]");
          const answer = frame.querySelector<HTMLElement>("[data-faq-answer]");
          const code = frame.querySelector<HTMLElement>("[data-faq-code]");
          const chart = frame.querySelector<HTMLElement>("[data-faq-chart]");
          const references = frame.querySelector<HTMLElement>("[data-faq-references]");
          const questionDuration = question?.textContent && question.textContent.length > 92 ? 1.28 : 0.96;
          const typingAt = frameStart + 0.1 + questionDuration + 0.08;
          const answerAt = typingAt + 0.72;
          const isApplicationFrame = scene.key === "applications" && frameIndex === 0;
          const referencesAt = answerAt + (chart ? 1.35 : code ? 0.85 : 0.28);
          const pointerAt = isApplicationFrame ? Math.max(referencesAt + 0.6, frameStart + 3) : null;
          const applicationReturnAt = pointerAt === null
            ? null
            : pointerAt + POINTER_TRAVEL_DURATION + POINTER_PRESS_DURATION + APPLICATION_LOADING_DURATION + APPLICATION_PREVIEW_DURATION;
          const isSetupCodeFrame = scene.key === "setup" && frameIndex === 1 && !!code;
          const copyPointerAt = isSetupCodeFrame ? Math.max(referencesAt + 0.4, frameStart + 3) : null;
          const copyClickAt = copyPointerAt === null
            ? null
            : copyPointerAt + POINTER_TRAVEL_DURATION + COPY_BUTTON_HOVER_DURATION;
          const isUsageChartFrame = scene.key === "usage" && frameIndex === 0 && !!chart;
          const chartPointerAt = isUsageChartFrame ? Math.max(referencesAt + 0.18, frameStart + 3.7) : null;
          const firstChartHoverAt = chartPointerAt === null ? null : chartPointerAt + CHART_POINTER_TRAVEL_DURATION;
          const secondChartMoveAt = firstChartHoverAt === null ? null : firstChartHoverAt + CHART_POINT_HOVER_DURATION;
          const secondChartHoverAt = secondChartMoveAt === null ? null : secondChartMoveAt + CHART_POINTER_TRAVEL_DURATION;
          const chartHoverEndAt = secondChartHoverAt === null ? null : secondChartHoverAt + CHART_POINT_HOVER_DURATION;
          const frameDuration = applicationReturnAt === null
            ? code ? 6.15 : chart ? 6 : 4.85
            : applicationReturnAt - frameStart - SCENE_START_DELAY;

          timeline.call(() => updateLocation(sceneIndex, frameIndex), [], frameStart);
          timeline.set(frame, { autoAlpha: 1, y: 0 }, frameStart);
          if (question) timeline.to(question, { clipPath: "inset(0 0% 0 0)", duration: questionDuration, ease: "none" }, frameStart + 0.1);
          if (typing) {
            timeline.to(typing, { autoAlpha: 1, y: 0, duration: 0.16 }, typingAt);
            timeline.to(typing, { autoAlpha: 0, duration: 0.16 }, typingAt + 0.48);
          }
          if (answer) timeline.to(answer, { autoAlpha: 1, y: 0, duration: 0.38 }, answerAt);
          if (code) timeline.to(code, { autoAlpha: 1, y: 0, duration: 0.38 }, answerAt + 0.34);
          if (chart) {
            const area = chart.querySelector<HTMLElement>("[data-faq-chart-area]");
            const line = chart.querySelector<SVGPathElement>("[data-faq-chart-line]");
            const reveal = chart.querySelector<SVGRectElement>("[data-faq-chart-reveal]");
            const points = Array.from(chart.querySelectorAll<SVGCircleElement>("[data-faq-chart-point]"));
            const drawStart = answerAt + 0.2;
            if (area) timeline.to(area, { autoAlpha: 1, duration: 0.34 }, drawStart);
            if (reveal) timeline.to(reveal, {
              attr: { y: CHART_REVEAL_TOP_Y, height: CHART_REVEAL_HEIGHT },
              duration: CHART_DRAW_DURATION,
              ease: "power2.out",
            }, drawStart);
            if (line) timeline.to(line, {
              strokeDashoffset: 0,
              duration: CHART_DRAW_DURATION,
              ease: "power1.out",
            }, drawStart);
            points.forEach((point, index) => {
              const targetY = Number(point.dataset.faqChartTargetY);
              const lineProgress = CHART_DRAW_DURATION * index / Math.max(points.length - 1, 1);
              const pointStart = Math.max(drawStart, drawStart + lineProgress - CHART_POINT_RISE_DURATION);
              timeline.to(point, {
                attr: { cy: targetY, r: 3.2 },
                duration: CHART_POINT_RISE_DURATION,
                ease: "back.out(1.3)",
              }, pointStart);
            });
          }
          if (references) timeline.to(references, { autoAlpha: 1, y: 0, duration: 0.32 }, referencesAt);

          if (isApplicationFrame && pointer && pointerAt !== null && applicationReturnAt !== null) {
            const clickAt = pointerAt + POINTER_TRAVEL_DURATION;
            const openAt = clickAt + POINTER_PRESS_DURATION;
            const returnAt = applicationReturnAt;

            timeline.call(() => {
              const targetPoint = measureFaqTarget();
              if (skipAutomaticPreviewRef.current || !targetPoint) {
                gsap.set(pointer, { autoAlpha: 0 });
                return;
              }
              gsap.set(pointer, {
                x: Math.max(16, root.clientWidth - 86),
                y: Math.max(72, root.clientHeight - 128),
                scale: 1,
                autoAlpha: 1,
                transformOrigin: "top left",
              });
            }, [], pointerAt);

            timeline.to(pointer, {
              x: () => measureFaqTarget()?.x ?? 0,
              y: () => measureFaqTarget()?.y ?? 0,
              duration: POINTER_TRAVEL_DURATION,
              ease: "power2.inOut",
            }, pointerAt);
            timeline.to(pointer, { scale: 0.82, duration: POINTER_PRESS_DURATION, ease: "power1.in" }, clickAt);
            timeline.call(() => {
              if (skipAutomaticPreviewRef.current || !measureFaqTarget()) return;
              const target = root.querySelector<HTMLElement>('[data-chat-action="demo-faq-assistant"]');
              if (!target) return;
              automaticActivationRef.current = true;
              try {
                target.click();
              } finally {
                automaticActivationRef.current = false;
              }
            }, [], openAt);
            timeline.to(pointer, { scale: 1, autoAlpha: 0, duration: 0.12, ease: "power1.out" }, openAt);
            timeline.call(() => {
              if (!automaticPreviewRef.current) return;
              automaticPreviewRef.current = false;
              setPreviewMode("chat");
            }, [], returnAt);
          }

          if (isSetupCodeFrame && pointer && copyPointerAt !== null && copyClickAt !== null) {
            const pointerArrivesAt = copyPointerAt + POINTER_TRAVEL_DURATION;

            timeline.call(() => {
              const targetPoint = measureCopyTarget();
              if (!targetPoint) {
                gsap.set(pointer, { autoAlpha: 0 });
                return;
              }
              gsap.set(pointer, {
                x: Math.max(16, root.clientWidth - 86),
                y: Math.max(72, root.clientHeight - 128),
                scale: 1,
                autoAlpha: 1,
                transformOrigin: "top left",
              });
            }, [], copyPointerAt);
            timeline.to(pointer, {
              x: () => measureCopyTarget()?.x ?? 0,
              y: () => measureCopyTarget()?.y ?? 0,
              duration: POINTER_TRAVEL_DURATION,
              ease: "power2.inOut",
            }, copyPointerAt);
            timeline.call(() => {
              rootElement.querySelector<HTMLElement>("[data-faq-demo-copy]")?.setAttribute("data-faq-demo-hover", "true");
            }, [], pointerArrivesAt);
            timeline.to(pointer, { scale: 0.82, duration: POINTER_PRESS_DURATION, ease: "power1.in" }, copyClickAt);
            timeline.call(() => {
              rootElement.querySelector<HTMLButtonElement>("[data-faq-demo-copy]")?.click();
            }, [], copyClickAt);
            timeline.to(pointer, { scale: 1, autoAlpha: 0, duration: 0.12, ease: "power1.out" }, copyClickAt + POINTER_PRESS_DURATION);
            timeline.call(clearCopyButtonHover, [], copyClickAt + POINTER_PRESS_DURATION);
          }

          if (
            isUsageChartFrame && pointer && chart && chartPointerAt !== null &&
            firstChartHoverAt !== null && secondChartMoveAt !== null &&
            secondChartHoverAt !== null && chartHoverEndAt !== null
          ) {
            const [firstIndex, secondIndex] = CHART_HOVER_POINT_INDICES;
            const firstPoint = chart.querySelector<SVGCircleElement>(`[data-faq-chart-index="${firstIndex}"]`);
            const secondPoint = chart.querySelector<SVGCircleElement>(`[data-faq-chart-index="${secondIndex}"]`);
            const firstTooltip = chart.querySelector<SVGGElement>(`[data-faq-chart-tooltip="${firstIndex}"]`);
            const secondTooltip = chart.querySelector<SVGGElement>(`[data-faq-chart-tooltip="${secondIndex}"]`);

            if (firstPoint && secondPoint && firstTooltip && secondTooltip) {
              timeline.call(() => {
                const targetPoint = measureTarget(`[data-faq-chart-index="${firstIndex}"]`);
                if (!targetPoint) {
                  gsap.set(pointer, { autoAlpha: 0 });
                  return;
                }
                gsap.set(pointer, {
                  x: Math.max(16, root.clientWidth - 86),
                  y: Math.max(72, root.clientHeight - 128),
                  scale: 1,
                  autoAlpha: 1,
                  transformOrigin: "top left",
                });
              }, [], chartPointerAt);
              timeline.to(pointer, {
                x: () => measureTarget(`[data-faq-chart-index="${firstIndex}"]`)?.x ?? 0,
                y: () => measureTarget(`[data-faq-chart-index="${firstIndex}"]`)?.y ?? 0,
                duration: CHART_POINTER_TRAVEL_DURATION,
                ease: "power2.inOut",
              }, chartPointerAt);
              timeline.to(firstPoint, {
                attr: { r: 5.8 },
                fill: "#bfdbfe",
                duration: 0.12,
                repeat: 3,
                yoyo: true,
                ease: "sine.inOut",
              }, firstChartHoverAt);
              timeline.to(firstTooltip, { autoAlpha: 1, duration: 0.14 }, firstChartHoverAt);
              timeline.to(firstTooltip, { autoAlpha: 0, duration: 0.12 }, secondChartMoveAt);
              timeline.to(pointer, {
                x: () => measureTarget(`[data-faq-chart-index="${secondIndex}"]`)?.x ?? 0,
                y: () => measureTarget(`[data-faq-chart-index="${secondIndex}"]`)?.y ?? 0,
                duration: CHART_POINTER_TRAVEL_DURATION,
                ease: "power2.inOut",
              }, secondChartMoveAt);
              timeline.to(secondPoint, {
                attr: { r: 5.8 },
                fill: "#bfdbfe",
                duration: 0.12,
                repeat: 3,
                yoyo: true,
                ease: "sine.inOut",
              }, secondChartHoverAt);
              timeline.to(secondTooltip, { autoAlpha: 1, duration: 0.14 }, secondChartHoverAt);
              timeline.to(secondTooltip, { autoAlpha: 0, duration: 0.12 }, chartHoverEndAt);
              timeline.to(pointer, { autoAlpha: 0, duration: 0.12, ease: "power1.out" }, chartHoverEndAt);
            }
          }

          if (frameIndex < sceneFrames.length - 1) timeline.to(frame, { autoAlpha: 0, y: -5, duration: 0.28 }, frameStart + frameDuration - 0.28);
          frameStart += frameDuration;
        });

        timeline.to(panel, { autoAlpha: 0, y: -6, duration: 0.32 }, frameStart - 0.32);
        sceneStart = frameStart + SCENE_START_DELAY;
      });

      let intersects = false;
      const syncPlayback = () => {
        if (document.hidden || !intersects) timeline.pause();
        else timeline.play();
      };
      const observer = typeof IntersectionObserver === "undefined"
        ? null
        : new IntersectionObserver(([entry]) => {
            intersects = !!entry?.isIntersecting;
            syncPlayback();
          }, { threshold: 0.18 });
      if (observer) observer.observe(root);
      else {
        intersects = true;
        syncPlayback();
      }
      document.addEventListener("visibilitychange", syncPlayback);

      return () => {
        observer?.disconnect();
        document.removeEventListener("visibilitychange", syncPlayback);
        timeline.kill();
        if (timelineRef.current === timeline) timelineRef.current = null;
      };
    });

    return () => media.revert();
  }, { scope: rootRef });

  return (
    <div
      ref={rootRef}
      className={`${className} ${styles.chatVisual}`}
      data-motion="feature-visual"
      data-preview-mode={previewMode}
      role="region"
      aria-label={t("landing.faqIllustrationAria")}
    >
      <div className={styles.windowBar}>
        <span className={styles.windowDots} aria-hidden="true"><i /><i /><i /></span>
        <span>{previewMode === "chat" ? "gateway-assistant" : "workspace.taskway.ai / applications"}</span>
        <b>{previewMode === "application" ? "APPLICATION · PREVIEW" : previewMode === "loading" ? "LOADING" : t("landing.faqDemo")}</b>
      </div>

      <div className={styles.contentStage}>
        <div
          className={`${styles.chatSurface} ${previewMode === "application" ? styles.chatSurfaceHidden : ""}`}
          aria-hidden={previewMode !== "chat"}
        >
          <div className={styles.chatHeader}>
            <span className={styles.avatar} aria-hidden="true"><TaskwayAssistantMark /></span>
            <span className={styles.agentIdentity}>
              <strong>Gateway Assistant</strong>
              <small><i />{copy.ready} · {t("landing.faqKnowledgeBase")}</small>
            </span>
            <span className={styles.headerTag}>FAQ</span>
          </div>

          <div className={styles.sceneViewport}>
            {scenes.map((scene, sceneIndex) => (
              <article
                className={`${styles.scene} ${activeLocation.scene === sceneIndex ? styles.sceneActive : ""}`}
                data-faq-scene={scene.key}
                aria-hidden={activeLocation.scene !== sceneIndex}
                key={scene.key}
              >
                <div className={styles.frameStage}>
                  {scene.turns.map((turn, frameIndex) => (
                    <div
                      className={`${styles.frame} ${activeLocation.scene === sceneIndex && activeLocation.frame === frameIndex ? styles.frameActive : ""}`}
                      data-faq-frame
                      aria-hidden={activeLocation.scene !== sceneIndex || activeLocation.frame !== frameIndex}
                      key={`${scene.key}-${frameIndex}`}
                    >
                      <div className={styles.userMessage}>
                        <span data-faq-question>{turn.question}</span>
                      </div>
                      <div className={styles.typing} data-faq-typing aria-hidden="true">
                        <TaskwayAssistantMark />
                        <span><i /><i /><i /></span>
                      </div>
                      <div className={styles.assistantMessage} data-faq-answer>
                        <SupportMessageContent content={turn.answer} />
                        {turn.code && <div data-faq-code><SupportMessageContent content={turn.code} demoCopy={scene.key === "setup"} /></div>}
                        {turn.chart && <LandingRequestChart label={copy.chartTitle} />}
                        <div data-faq-references>
                          <SupportChatReferences
                            sources={[turn.source]}
                            actions={turn.actions}
                          />
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </article>
            ))}
          </div>

          <nav className={styles.sceneNavigation} aria-label={t("landing.faqFollowUp")}>
            {scenes.map((scene, index) => (
              <button
                className={styles.sceneButton}
                data-faq-indicator
                aria-pressed={activeLocation.scene === index}
                aria-label={`${copy.scene} ${index + 1}: ${scene.label}`}
                key={scene.key}
                onClick={() => selectScene(index)}
                type="button"
              >
                <i aria-hidden="true" />{scene.label}
              </button>
            ))}
          </nav>

          <div className={styles.composer} aria-hidden="true">
            <span>{t("landing.faqComposerPlaceholder")}</span>
            <span className={styles.sendButton}>
              <svg viewBox="0 0 24 24" fill="none"><path d="m5 12 14-7-4 14-3-5-7-2Z" /><path d="m12 14 3-3" /></svg>
            </span>
          </div>
        </div>

        {previewMode !== "chat" && <div className={styles.applicationStage}>
          {previewMode === "loading"
            ? <LandingApplicationLoading />
            : <LandingApplicationPreview onBack={returnToChat} />}
        </div>}
      </div>

      <div className={styles.pointer} data-faq-pointer aria-hidden="true">
        <svg viewBox="0 0 26 34" focusable="false">
          <path d="M2 2v25l6.1-6.1 4.8 10.6 4.7-2.1-4.7-10.6 9.1.3L2 2Z" />
        </svg>
      </div>
    </div>
  );
}
