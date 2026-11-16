"use client";

import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react";
import Link from "next/link";
import { gsap } from "gsap";
import { DrawSVGPlugin } from "gsap/DrawSVGPlugin";
import { MotionPathPlugin } from "gsap/MotionPathPlugin";
import { useGSAP } from "@gsap/react";

import { useLanguage } from "@/components/language-provider";
import styles from "./landing-product-features.module.css";

gsap.registerPlugin(useGSAP, DrawSVGPlugin, MotionPathPlugin);

type SlideKind = "flow" | "hard" | "tiers" | "vietnamese" | "efficiency";
type Translate = ReturnType<typeof useLanguage>["t"];
type TranslationKey = Parameters<Translate>[0];
const SLIDE_KINDS: SlideKind[] = ["flow", "hard", "tiers", "vietnamese", "efficiency"];
const KEYWORD_KEYS: Partial<Record<SlideKind, TranslationKey>> = {
  hard: "landing.routingHardKeyword",
  tiers: "landing.routingTiersKeyword",
  vietnamese: "landing.routingVietnameseKeyword",
  efficiency: "landing.routingEfficiencyKeyword",
};
const SLIDE_DURATION_MS = 6500;

function slideKey(kind: SlideKind, part: "Kicker" | "Title" | "Description") {
  const prefix = kind === "flow" ? "landing.routing" : "landing.routing" + kind[0].toUpperCase() + kind.slice(1);
  return (prefix + part) as TranslationKey;
}

function animatePacket(timeline: gsap.core.Timeline, packet: SVGCircleElement | null, path: SVGPathElement | null, position: number, duration: number) {
  if (!packet || !path) return;
  timeline.fromTo(packet, { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.12 }, position);
  timeline.to(packet, { motionPath: { path, align: path, alignOrigin: [0.5, 0.5] }, duration, ease: "none" }, position);
  timeline.to(packet, { autoAlpha: 0, duration: 0.12 }, position + duration);
}

function EmphasizedTitle({ text, keyword }: { text: string; keyword: string }) {
  const index = keyword ? text.indexOf(keyword) : -1;
  if (index < 0) return text;
  return <>{text.slice(0, index)}<strong className={styles.routingKeyword}>{keyword}</strong>{text.slice(index + keyword.length)}</>;
}

function RoutingDiagram({ t }: { t: Translate }) {
  return (
    <div className={styles.routingVisual}>
      <div className={styles.routingWindowBar}>
        <span className={styles.windowDots} aria-hidden="true"><i /><i /><i /></span><span>subtask-routing.flow</span><b>v1</b>
      </div>
      <svg className={styles.routingDiagram} data-routing-diagram role="img" aria-label={t("landing.routingAria")} viewBox="0 0 680 560">
        <defs>
          <linearGradient id="routeGreen" x1="0" x2="1"><stop stopColor="#53b994" /><stop offset="1" stopColor="#4b91dc" /></linearGradient>
          <marker id="arrowPurple" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 8 4 0 8z" fill="#6b57dc" /></marker>
          <filter id="packetGlow" x="-250%" y="-250%" width="500%" height="500%"><feGaussianBlur stdDeviation="3" result="blur" /><feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
        </defs>
        <g aria-hidden="true">
          <path className={styles.packetRoute} data-routing-route="blue" d="M340 74V194" />
          <path className={styles.packetRoute} data-routing-route="green" d="M340 234C340 304 160 300 155 341V439C155 469 270 474 340 482V516" />
          <path className={styles.packetRoute} data-routing-route="amber" d="M340 234V516" />
          <path className={styles.packetRoute} data-routing-route="purple" d="M340 234C340 304 520 300 525 341V439C525 469 410 474 340 482V516" />
          <path className={styles.packetRoute} data-routing-route="fallback" d="M370 418H490" />
        </g>
        <path className={styles.tierRule} d="M76 413H604" />
        <g>
          <path className={styles.flowPath} data-routing-path="request-engine" d="M340 74V101" />
          <path className={styles.flowPath} data-routing-path="engine-decision" d="M340 169V194" />
          <path className={styles.flowPath} data-routing-path="task-a" d="M340 234C340 304 160 300 155 341" />
          <path className={styles.flowPath} data-routing-path="task-b" d="M340 234V341" />
          <path className={styles.flowPath} data-routing-path="task-c" d="M340 234C340 304 520 300 525 341" />
          <path className={styles.flowPath} data-routing-path="model-a" d="M155 398V439" />
          <path className={styles.flowPath} data-routing-path="model-b" d="M340 398V423" />
          <path className={styles.flowPath} data-routing-path="model-c" d="M525 398V416" />
          <path className={styles.flowPath} data-routing-path="response-a" d="M155 479C155 469 270 474 340 482" />
          <path className={styles.flowPath} data-routing-path="response-b" d="M340 485V516" />
          <path className={styles.flowPath} data-routing-path="response-c" d="M525 492C525 469 410 474 340 482" />
          <path className={styles.fallbackPath} data-routing-path="fallback" d="M371 418H490" markerEnd="url(#arrowPurple)" />
        </g>
        <g className={styles.packetLayer} aria-hidden="true">
          <circle className={styles.packetBlue} data-routing-packet="blue" cx="0" cy="0" r="4" filter="url(#packetGlow)" />
          <circle className={styles.packetGreen} data-routing-packet="green" cx="0" cy="0" r="4" filter="url(#packetGlow)" />
          <circle className={styles.packetAmber} data-routing-packet="amber" cx="0" cy="0" r="4" filter="url(#packetGlow)" />
          <circle className={styles.packetPurple} data-routing-packet="purple" cx="0" cy="0" r="4" filter="url(#packetGlow)" />
          <circle className={styles.packetFallback} data-routing-packet="fallback" cx="0" cy="0" r="3.5" filter="url(#packetGlow)" />
        </g>
        <g data-routing-node="request">
          <rect className={styles.requestNode} x="262" y="40" width="156" height="34" rx="17" /><text className={styles.nodeText} x="340" y="61" textAnchor="middle">{t("landing.diagramRequest")}</text>
        </g>
        <g data-routing-node="engine">
          <rect className={styles.engineNode} x="234" y="102" width="212" height="67" rx="13" /><text className={styles.engineLabel} x="264" y="132">jev</text>
          <rect className={styles.barOne} data-routing-engine-bar x="337" y="130" width="11" height="17" rx="2" /><rect className={styles.barTwo} data-routing-engine-bar x="360" y="120" width="11" height="27" rx="2" /><rect className={styles.barThree} data-routing-engine-bar x="383" y="127" width="11" height="20" rx="2" /><rect className={styles.barFour} data-routing-engine-bar x="406" y="118" width="11" height="29" rx="2" />
        </g>
        <g data-routing-node="decision">
          <path className={styles.decisionNode} d="M340 194 383 214 340 234 297 214z" /><text className={styles.nodeText} x="340" y="218" textAnchor="middle">{t("landing.diagramSplit")}</text>
          <text className={styles.greenHint} x="157" y="211" textAnchor="middle">{t("landing.diagramMultiGoal")}</text><text className={styles.greenHint} x="157" y="226" textAnchor="middle">{t("landing.diagramSplitArrow")}</text>
          <text className={styles.mutedHint} x="525" y="211" textAnchor="middle">{t("landing.diagramSingleVagueCostly")}</text><text className={styles.mutedHint} x="525" y="226" textAnchor="middle">{t("landing.diagramKeepOne")}</text>
        </g>
        <g data-routing-node="task-a">
          <rect className={styles.subtaskGreen} x="98" y="341" width="114" height="57" rx="11" /><text className={styles.subtaskText} x="155" y="364" textAnchor="middle">{t("landing.diagramSubtaskA")}</text>
          <circle className={styles.dotGreen} cx="142" cy="380" r="4" /><circle className={styles.emptyDot} cx="155" cy="380" r="4" /><circle className={styles.emptyDot} cx="168" cy="380" r="4" />
        </g>
        <g data-routing-node="task-b">
          <rect className={styles.subtaskAmber} x="283" y="341" width="114" height="57" rx="11" /><text className={styles.subtaskText} x="340" y="364" textAnchor="middle">{t("landing.diagramSubtaskB")}</text>
          <circle className={styles.dotAmber} cx="327" cy="380" r="4" /><circle className={styles.dotAmber} cx="340" cy="380" r="4" /><circle className={styles.emptyDot} cx="353" cy="380" r="4" />
        </g>
        <g data-routing-node="task-c">
          <rect className={styles.subtaskPurple} x="468" y="341" width="114" height="57" rx="11" /><text className={styles.subtaskText} x="525" y="364" textAnchor="middle">{t("landing.diagramSubtaskC")}</text>
          <circle className={styles.dotPurple} cx="512" cy="380" r="4" /><circle className={styles.dotPurple} cx="525" cy="380" r="4" /><circle className={styles.dotPurple} cx="538" cy="380" r="4" />
        </g>
        <g data-routing-node="model-a"><circle className={styles.smallModel} cx="155" cy="454" r="25" /><text className={styles.modelLetterGreen} x="155" y="458" textAnchor="middle">S</text><text className={styles.modelCaptionGreen} x="155" y="503" textAnchor="middle">small_fast</text></g>
        <g data-routing-node="model-b"><circle className={styles.midModel} cx="340" cy="454" r="31" /><text className={styles.modelLetterAmber} x="340" y="458" textAnchor="middle">M</text><text className={styles.modelCaptionAmber} x="340" y="510" textAnchor="middle">mid</text></g>
        <g data-routing-node="model-c"><circle className={styles.largeModel} cx="525" cy="454" r="38" /><text className={styles.modelLetterPurple} x="525" y="458" textAnchor="middle">L</text><text className={styles.modelCaptionPurple} x="525" y="518" textAnchor="middle">large_reasoning</text></g>
        <g data-routing-node="fallback-label"><text className={styles.fallbackLabel} x="430" y="410" textAnchor="middle">{t("landing.diagramFallback")}</text></g>
        <g data-routing-node="response"><rect className={styles.responseNode} x="242" y="516" width="196" height="32" rx="16" /><text className={styles.nodeText} x="340" y="536" textAnchor="middle">{t("landing.diagramAggregateResponse")}</text></g>
      </svg>
      <div className={styles.routingLegend}><span aria-hidden="true" /> {t("landing.routingLegend")}</div>
    </div>
  );
}

function BarFill({ color, value }: { color: "primary" | "secondary" | "muted"; value: number }) {
  const colorClass = color === "primary" ? styles.routingBarFillPrimary : color === "secondary" ? styles.routingBarFillSecondary : styles.routingBarFillMuted;
  return <span className={[styles.routingBarFill, colorClass].join(" ")} data-routing-bar style={{ width: value + "%" }} />;
}

function SlideVisual({ kind, t }: { kind: SlideKind; t: Translate }) {
  if (kind === "flow") return <RoutingDiagram t={t} />;
  if (kind === "hard") {
    const rows = [
      { label: t("landing.routingHardRouter"), value: 97.9, color: "primary" as const, result: t("landing.routingHardRouterValue") },
      { label: t("landing.routingHardSol"), value: 91.7, color: "secondary" as const, result: t("landing.routingHardSolValue") },
      { label: t("landing.routingHardSmall"), value: 67.7, color: "muted" as const, result: t("landing.routingHardSmallValue") },
    ];
    return <div className={styles.routingMetricVisual}>
      <div className={styles.routingVisualHeading} data-routing-reveal><span>{t("landing.routingHardScoreLabel")}</span><small>HARDER_CASES · %</small></div>
      <div className={styles.routingBarList}>{rows.map((row) => <div className={styles.routingBarRow} data-routing-reveal key={row.label}><div className={styles.routingBarMeta}><span>{row.label}</span><b>{row.result}</b></div><div className={styles.routingBarTrack}><BarFill color={row.color} value={row.value} /></div></div>)}</div>
      <p className={styles.routingFootnote} data-routing-reveal>{t("landing.routingHardFootnote")}</p>
    </div>;
  }
  if (kind === "tiers") return <div className={[styles.routingMetricVisual, styles.routingTierVisual].join(" ")}>
    <article className={styles.routingTierCard} data-routing-reveal><span className={styles.routingTierLabel}><i className={styles.routingTierDotSmall} />{t("landing.routingTierEasy")}</span><strong><b>{t("landing.routingTierEasyValue")}</b></strong><span className={styles.routingTierOutcome}>{t("landing.routingTierSmallOutcome")}</span><span className={styles.routingTierTrack}><i data-routing-bar style={{ width: "96.7%" }} /></span></article>
    <article className={styles.routingTierCard} data-routing-reveal><span className={styles.routingTierLabel}><i className={styles.routingTierDotLarge} />{t("landing.routingTierHard")}</span><strong><b>{t("landing.routingTierHardValue")}</b></strong><span className={styles.routingTierOutcome}>{t("landing.routingTierUpOutcome")}</span><span className={styles.routingTierTrack}><i data-routing-bar style={{ width: "100%" }} /></span></article>
    <p className={styles.routingFootnote} data-routing-reveal>{t("landing.routingTierFootnote")}</p>
  </div>;
  if (kind === "vietnamese") {
    const rows = [
      { label: t("landing.routingVietnameseRouter"), value: 86.9, color: "primary" as const, result: t("landing.routingVietnameseRouterValue") },
      { label: t("landing.routingVietnameseOld"), value: 81.1, color: "secondary" as const, result: t("landing.routingVietnameseOldValue") },
      { label: t("landing.routingVietnameseSmall"), value: 50, color: "muted" as const, result: t("landing.routingVietnameseSmallValue") },
    ];
    return <div className={styles.routingMetricVisual}>
      <div className={styles.routingVisualHeading} data-routing-reveal><span>{t("landing.routingVietnameseScoreLabel")}</span><small>N=450 · VI</small></div>
      <div className={styles.routingBarList}>{rows.map((row) => <div className={styles.routingBarRow} data-routing-reveal key={row.label}><div className={styles.routingBarMeta}><span>{row.label}</span><b>{row.result}</b></div><div className={styles.routingBarTrack}><BarFill color={row.color} value={row.value} /></div></div>)}</div>
      <p className={styles.routingFootnote} data-routing-reveal>{t("landing.routingVietnameseFootnote")}</p>
    </div>;
  }
  return <div className={[styles.routingMetricVisual, styles.routingEfficiencyVisual].join(" ")}>
    <div className={styles.routingEfficiencyMatch} data-routing-reveal>
      <div className={styles.routingMetricCardHeading}><span>{t("landing.routingEfficiencyTierMatch")}</span></div>
      <div className={styles.routingTierComparison}>
        <div className={styles.routingTierCurrent}><span>{t("landing.routingEfficiencyExisting")}</span><strong>78%</strong></div>
        <span className={styles.routingCompareArrow} aria-hidden="true">→</span>
        <div className={styles.routingTierProposed}><span>{t("landing.routingEfficiencyProposed")}</span><strong>100%</strong></div>
      </div>
    </div>
    <div className={styles.routingCostCard} data-routing-reveal>
      <div className={styles.routingCostHeading}><span>{t("landing.routingEfficiencyCost")} · {t("landing.routingEfficiencyPerMillion")}</span><b>−39%</b></div>
      <div className={styles.routingCostCompare}>
        <div className={styles.routingCostCurrent}><span>{t("landing.routingEfficiencyExisting")}</span><strong>$0.93</strong><i className={styles.routingCostTrack}><i style={{ width: "100%" }} /></i></div>
        <span className={styles.routingCompareArrow} aria-hidden="true">→</span>
        <div className={styles.routingCostProposed}><span>{t("landing.routingEfficiencyProposed")}</span><strong>$0.57</strong><i className={styles.routingCostTrack}><i style={{ width: "61.3%" }} /></i></div>
      </div>
    </div>
  </div>;
}

export function LandingRoutingShowcase() {
  const { t } = useLanguage();
  const rootRef = useRef<HTMLElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const chartTimelineRef = useRef<gsap.core.Timeline | null>(null);
  const packetTimelineRef = useRef<gsap.core.Timeline | null>(null);
  const activeIndexRef = useRef(0);
  const navigateRef = useRef<(index: number) => void>(() => undefined);
  const pointerStartRef = useRef<number | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const timer = window.setTimeout(() => {
      navigateRef.current((activeIndex + 1) % SLIDE_KINDS.length);
    }, SLIDE_DURATION_MS);
    return () => window.clearTimeout(timer);
  }, [activeIndex]);

  useGSAP((_, contextSafe) => {
    const root = rootRef.current;
    const track = trackRef.current;
    if (!root || !track) return;
    let inView = false;
    const syncChart = () => {
      const intro = chartTimelineRef.current;
      const packets = packetTimelineRef.current;
      if (!intro || !packets) return;
      if (activeIndexRef.current === 0 && inView) {
        if (intro.progress() < 1) intro.play();
        else if (!packets.isActive()) packets.play();
      } else {
        intro.pause(0);
        packets.pause(0);
      }
    };

    const media = gsap.matchMedia();
    media.add("(prefers-reduced-motion: no-preference)", () => {
      const chart = root.querySelector<SVGSVGElement>("[data-routing-diagram]");
      if (!chart) return;
      const path = (name: string) => chart.querySelector<SVGPathElement>('[data-routing-path="' + name + '"]');
      const route = (name: string) => chart.querySelector<SVGPathElement>('[data-routing-route="' + name + '"]');
      const node = (name: string) => chart.querySelector<SVGGElement>('[data-routing-node="' + name + '"]');
      const packet = (name: string) => chart.querySelector<SVGCircleElement>('[data-routing-packet="' + name + '"]');
      const taskPaths = [path("task-a"), path("task-b"), path("task-c")].filter((item): item is SVGPathElement => Boolean(item));
      const modelPaths = [path("model-a"), path("model-b"), path("model-c")].filter((item): item is SVGPathElement => Boolean(item));
      const responsePaths = [path("response-a"), path("response-b"), path("response-c")].filter((item): item is SVGPathElement => Boolean(item));
      const timeline = gsap.timeline({ paused: true });
      const packetTimeline = gsap.timeline({ paused: true, repeat: -1, repeatDelay: 0.7 });
      const appear = (target: Element | null, at: number, duration = 0.36) => {
        if (target) timeline.fromTo(target, { autoAlpha: 0, scale: 0.94, transformOrigin: "50% 50%" }, { autoAlpha: 1, scale: 1, duration, ease: "back.out(1.25)" }, at);
      };
      const draw = (target: SVGPathElement | null, at: number, duration = 0.5) => {
        if (target) timeline.fromTo(target, { drawSVG: "0% 0%" }, { drawSVG: "0% 100%", duration, ease: "none" }, at);
      };
      appear(node("request"), 0, 0.34);
      draw(path("request-engine"), 0.08, 0.52);
      appear(node("engine"), 0.48);
      draw(path("engine-decision"), 0.72, 0.48);
      appear(node("decision"), 1.06, 0.38);
      if (taskPaths.length) timeline.fromTo(taskPaths, { drawSVG: "0% 0%" }, { drawSVG: "0% 100%", duration: 0.74, stagger: 0.14, ease: "none" }, 1.16);
      const taskNodes = [node("task-a"), node("task-b"), node("task-c")].filter((item): item is SVGGElement => Boolean(item));
      if (taskNodes.length) timeline.fromTo(taskNodes, { autoAlpha: 0, scale: 0.94, transformOrigin: "50% 50%" }, { autoAlpha: 1, scale: 1, duration: 0.36, stagger: 0.12, ease: "back.out(1.2)" }, 1.72);
      if (modelPaths.length) timeline.fromTo(modelPaths, { drawSVG: "0% 0%" }, { drawSVG: "0% 100%", duration: 0.56, stagger: 0.1, ease: "none" }, 2.18);
      const modelNodes = [node("model-a"), node("model-b"), node("model-c")].filter((item): item is SVGGElement => Boolean(item));
      if (modelNodes.length) timeline.fromTo(modelNodes, { autoAlpha: 0, scale: 0.86, transformOrigin: "50% 50%" }, { autoAlpha: 1, scale: 1, duration: 0.4, stagger: 0.14, ease: "back.out(1.3)" }, 2.48);
      draw(path("fallback"), 2.94, 0.48);
      const fallbackLabel = node("fallback-label");
      if (fallbackLabel) timeline.fromTo(fallbackLabel, { autoAlpha: 0, y: 5 }, { autoAlpha: 1, y: 0, duration: 0.26, ease: "power2.out" }, 3.16);
      if (responsePaths.length) timeline.fromTo(responsePaths, { drawSVG: "0% 0%" }, { drawSVG: "0% 100%", duration: 0.62, stagger: 0.11, ease: "none" }, 3.38);
      appear(node("response"), 4.05);

      animatePacket(packetTimeline, packet("blue"), route("blue"), 0, 1.25);
      animatePacket(packetTimeline, packet("green"), route("green"), 0.58, 3.55);
      animatePacket(packetTimeline, packet("amber"), route("amber"), 0.78, 3.25);
      animatePacket(packetTimeline, packet("purple"), route("purple"), 0.98, 3.55);
      animatePacket(packetTimeline, packet("fallback"), route("fallback"), 2.2, 1.1);

      timeline.eventCallback("onComplete", () => packetTimeline.restart());
      chartTimelineRef.current = timeline;
      packetTimelineRef.current = packetTimeline;
      syncChart();
      return () => {
        if (chartTimelineRef.current === timeline) chartTimelineRef.current = null;
        if (packetTimelineRef.current === packetTimeline) packetTimelineRef.current = null;
        timeline.kill();
        packetTimeline.kill();
      };
    });

    const navigate = (requestedIndex: number) => {
      const nextIndex = (requestedIndex + SLIDE_KINDS.length) % SLIDE_KINDS.length;
      if (nextIndex === activeIndexRef.current) return;
      activeIndexRef.current = nextIndex;
      setActiveIndex(nextIndex);
      syncChart();
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const slide = track.children[nextIndex] as HTMLElement | undefined;
      const reveals = slide?.querySelectorAll<HTMLElement>("[data-routing-reveal]");
      const bars = slide?.querySelectorAll<HTMLElement>("[data-routing-bar]");
      if (reducedMotion) {
        gsap.set(track, { xPercent: (-100 * nextIndex) / SLIDE_KINDS.length });
        if (reveals?.length) gsap.set(reveals, { autoAlpha: 1, y: 0, scale: 1 });
        if (bars?.length) gsap.set(bars, { scaleX: 1, transformOrigin: "left center" });
        return;
      }
      const transition = gsap.timeline({ defaults: { ease: "power2.inOut" } });
      transition.to(track, { xPercent: (-100 * nextIndex) / SLIDE_KINDS.length, duration: 0.78, overwrite: "auto" }, 0);
      if (reveals?.length) transition.fromTo(reveals, { autoAlpha: 0, y: 15, scale: 0.985 }, { autoAlpha: 1, y: 0, scale: 1, duration: 0.42, stagger: 0.055, ease: "power2.out", overwrite: "auto" }, 0.32);
      if (bars?.length) transition.fromTo(bars, { scaleX: 0, transformOrigin: "left center" }, { scaleX: 1, duration: 0.78, stagger: 0.1, ease: "power2.out", overwrite: "auto" }, 0.42);
    };
    navigateRef.current = contextSafe ? contextSafe(navigate) : navigate;
    const observer = new IntersectionObserver(([entry]) => { inView = entry.isIntersecting; syncChart(); }, { threshold: 0.18 });
    observer.observe(root);
    return () => {
      observer.disconnect();
      media.revert();
      chartTimelineRef.current = null;
      navigateRef.current = () => undefined;
    };
  }, { scope: rootRef });

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "ArrowRight") { event.preventDefault(); navigateRef.current(activeIndex + 1); }
    if (event.key === "ArrowLeft") { event.preventDefault(); navigateRef.current(activeIndex - 1); }
  }
  function onPointerDown(event: PointerEvent<HTMLDivElement>) {
    if ((event.target as HTMLElement).closest("a, button")) return;
    pointerStartRef.current = event.clientX;
  }
  function onPointerUp(event: PointerEvent<HTMLDivElement>) {
    const startX = pointerStartRef.current;
    pointerStartRef.current = null;
    if (startX === null || Math.abs(event.clientX - startX) < 46) return;
    navigateRef.current(activeIndex + (event.clientX < startX ? 1 : -1));
  }

  return (
    <section className={[styles.feature, styles.routingFeature].join(" ")} data-motion="feature" id="routing" aria-labelledby={"routing-slide-heading-" + activeIndex} ref={rootRef}>
      <div className={styles.routingCarousel} data-motion="feature-visual">
        <div className={styles.routingViewport} role="region" aria-roledescription={t("landing.routingCarouselRole")} aria-label={t("landing.routingCarouselAria")} tabIndex={0} onKeyDown={onKeyDown} onPointerDown={onPointerDown} onPointerUp={onPointerUp} onPointerCancel={() => { pointerStartRef.current = null; }}>
          <div className={styles.routingTrack} ref={trackRef} style={{
            "--routing-track-width": `${SLIDE_KINDS.length * 100}%`,
            "--routing-slide-width": `${100 / SLIDE_KINDS.length}%`,
          } as CSSProperties}>
            {SLIDE_KINDS.map((kind, index) => {
              const active = index === activeIndex;
              const copy = { kicker: t(slideKey(kind, "Kicker")), title: t(slideKey(kind, "Title")), description: t(slideKey(kind, "Description")) };
              const keywordKey = KEYWORD_KEYS[kind];
              const keyword = keywordKey ? t(keywordKey) : "";
              return (
                <article className={[styles.routingSlide, kind === "flow" ? styles.routingFlowSlide : ""].join(" ")} data-routing-slide data-active={active ? "true" : "false"} key={kind} role="group" aria-roledescription={t("landing.routingSlideWord")} aria-label={t("landing.routingSlidePosition", { current: index + 1, total: SLIDE_KINDS.length })} aria-hidden={!active} inert={!active}>
                  <SlideVisual kind={kind} t={t} />
                  <div className={styles.copy}>
                    <p className={styles.kicker}>{copy.kicker}</p><h2 id={"routing-slide-heading-" + index}><EmphasizedTitle text={copy.title} keyword={keyword} /></h2><p>{copy.description}</p>
                    <div className={styles.routingActions}><Link className={styles.routingPrimaryAction} href="/login">{t("landing.routingTryNow")} <span aria-hidden="true">→</span></Link></div>
                  </div>
                </article>
              );
            })}
          </div>
        </div>
        <div className={styles.routingCarouselFooter}>
          <div className={styles.routingPagination} role="group" aria-label={t("landing.routingPagination")}>
            {SLIDE_KINDS.map((kind, index) => <button className={index === activeIndex ? styles.routingPageActive : styles.routingPageButton} type="button" key={kind} aria-label={t("landing.routingGoToSlide", { slide: index + 1 })} aria-pressed={index === activeIndex} onClick={() => navigateRef.current(index)}><span /></button>)}
          </div>
        </div>
      </div>
    </section>
  );
}
