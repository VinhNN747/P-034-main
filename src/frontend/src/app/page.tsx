"use client";

import { useRef } from "react";
import Link from "next/link";
import { useGSAP } from "@gsap/react";
import { gsap } from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";

import { LandingProductFeatures } from "@/components/landing-product-features";
import { LandingProductWalkthrough } from "@/components/landing-product-walkthrough";
import { LanguageToggle } from "@/components/language-toggle";
import { useLanguage } from "@/components/language-provider";
import { ThemeToggle } from "@/components/theme-toggle";
import { TaskwayLogo } from "@/components/taskway-logo";
import { taskwayDocsUrl } from "@/lib/taskway-docs";

gsap.registerPlugin(useGSAP, ScrollTrigger);

export default function Home() {
  const { t, language } = useLanguage();
  const landingRef = useRef<HTMLElement>(null);

  useGSAP(() => {
    const root = landingRef.current;
    if (!root) return;

    const media = gsap.matchMedia();
    media.add("(prefers-reduced-motion: no-preference)", () => {
      gsap.timeline({ defaults: { ease: "power3.out" } })
        .from("[data-motion='header']", { autoAlpha: 0, y: -12, duration: 0.48 })
        .from("[data-motion='hero-copy'] > *", {
          autoAlpha: 0,
          y: 20,
          duration: 0.62,
          stagger: 0.08,
        }, "-=0.14")
        .from("[data-motion='hero-visual']", {
          autoAlpha: 0,
          x: 24,
          y: 8,
          scale: 0.98,
          duration: 0.78,
        }, "-=0.46");

      root.querySelectorAll<HTMLElement>("[data-motion='feature']").forEach((section) => {
        const targets = section.querySelectorAll<HTMLElement>(
          "[data-motion='feature-copy'], [data-motion='feature-visual']",
        );

        gsap.fromTo(targets, {
          autoAlpha: 0,
          y: 24,
        }, {
          autoAlpha: 1,
          y: 0,
          duration: 0.76,
          stagger: 0.14,
          ease: "power3.out",
          scrollTrigger: {
            trigger: section,
            start: "top 78%",
            once: true,
          },
        });
      });

      const closingSection = root.querySelector<HTMLElement>("[data-motion='closing']");
      if (closingSection) {
        gsap.from(closingSection.children, {
          autoAlpha: 0,
          y: 18,
          duration: 0.62,
          stagger: 0.09,
          ease: "power3.out",
          scrollTrigger: {
            trigger: closingSection,
            start: "top 82%",
            once: true,
          },
        });
      }
    }, root);

    return () => media.revert();
  }, { scope: landingRef });

  return (
    <main ref={landingRef} className="gateway-site">
      <header className="site-header" data-motion="header">
        <Link className="gateway-brand site-brand" href="/" aria-label={t("landing.home")}>
          <TaskwayLogo className="taskway-logo--site" appearance="theme-aware" />
        </Link>

        <nav className="site-nav" aria-label={t("landing.mainNavigation")}>
          <a href="#sdk">{t("landing.navSdk")}</a>
          <a href="#routing">{t("landing.navRouting")}</a>
          <a href="#faq">{t("landing.navFaq")}</a>
          <a href={taskwayDocsUrl(language)} target="_blank" rel="noopener noreferrer">{t("landing.docs")}</a>
        </nav>

        <div className="site-preference-controls" role="group" aria-label={t("landing.preferenceControls")}>
          <LanguageToggle />
          <ThemeToggle compact />
        </div>
        <div className="header-actions">
          <Link className="quiet-link header-login" href="/login">{t("landing.signIn")}</Link>
          <Link className="header-cta" href="/login"><span className="header-cta-full">{t("landing.getStarted")}</span><span className="header-cta-short">{t("landing.startFree")}</span></Link>
        </div>
      </header>

      <section className="hero-section" aria-labelledby="hero-heading">
        <div className="hero-copy" data-motion="hero-copy">
          <h1 id="hero-heading">{t("landing.heroTitle")}</h1>
          <p className="hero-description">{t("landing.heroDescription")}</p>
          <div className="hero-actions">
            <Link className="primary-action" href="/login">{t("landing.createGateway")}</Link>
            <a className="secondary-action" href="#routing">{t("landing.viewRouting")}</a>
          </div>
        </div>

        <LandingProductWalkthrough />
      </section>

      <LandingProductFeatures />

      <section className="closing-section" data-motion="closing" id="security" aria-labelledby="closing-heading">
        <p className="section-label">{t("landing.closingLabel")}</p>
        <h2 id="closing-heading">{t("landing.closingTitle")}</h2>
        <p>{t("landing.closingDescription")}</p>
        <Link className="primary-action" href="/login">{t("landing.closingAction")}</Link>
      </section>

      <footer className="site-footer" id="docs">
        <Link className="gateway-brand site-brand" href="/" aria-label={t("landing.home")}><TaskwayLogo className="taskway-logo--footer" appearance="theme-aware" /></Link>
        <span>© 2026 Taskway - The Fellowship of the Ring</span>
        <span>{t("landing.footerDescription")}</span>
      </footer>
    </main>
  );
}
