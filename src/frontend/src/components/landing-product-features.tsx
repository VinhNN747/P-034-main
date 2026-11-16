"use client";

import Link from "next/link";

import { useLanguage } from "@/components/language-provider";
import { LandingRoutingShowcase } from "@/components/landing-routing-showcase";
import { LandingFaqWalkthrough } from "@/components/landing-faq-walkthrough";
import styles from "./landing-product-features.module.css";

export function LandingProductFeatures() {
  const { t } = useLanguage();

  return (
    <div className={styles.features}>
      <section className={styles.feature} data-motion="feature" id="sdk" aria-labelledby="sdk-heading">
        <div className={styles.copy} data-motion="feature-copy">
          <p className={styles.kicker}>{t("landing.sdkKicker")}</p>
          <h2 id="sdk-heading">{t("landing.sdkTitle")}</h2>
          <p>{t("landing.sdkDescription")}</p>
          <Link className={`primary-action ${styles.sdkAction}`} href="/sdk-guide">{t("landing.sdkAction")} <span aria-hidden="true">→</span></Link>
        </div>
        <div className={`${styles.visual} ${styles.sdkVisual}`} data-motion="feature-visual">
          <div className={styles.windowBar}><i /><i /><i /><span>quickstart.py</span><b>v1</b></div>
          <pre><code><span className={styles.keyword}>from</span> openai <span className={styles.keyword}>import</span> OpenAI{"\n\n"}<span className={styles.variable}>client</span> = OpenAI({"\n"}  api_key=os.environ[<em>&quot;TASKWAY_API_KEY&quot;</em>],{"\n"}  base_url=os.environ[<em>&quot;TASKWAY_BASE_URL&quot;</em>],{"\n"}){"\n\n"}<span className={styles.variable}>response</span> = client.responses.create({"\n"}  model=<em>&quot;auto&quot;</em>,{"\n"}  input=<em>&quot;Summarize this report&quot;</em>,{"\n"})</code></pre>
          <div className={styles.windowFoot}><span className={styles.liveDot} /> {t("landing.sdkWindowFoot")}</div>
        </div>
      </section>

      <LandingRoutingShowcase />

      <section className={`${styles.feature} ${styles.faqFeature}`} data-motion="feature" id="faq" aria-labelledby="faq-heading">
        <div className={styles.copy} data-motion="feature-copy">
          <p className={styles.kicker}>{t("landing.faqKicker")}</p>
          <h2 id="faq-heading">{t("landing.faqTitle")}</h2>
          <p>{t("landing.faqDescription")}</p>
          <ul className={styles.faqCapabilities} aria-label={t("landing.faqCapabilitiesAria")}>
            <li>{t("landing.faqCapabilitySemantic")}</li>
            <li>{t("landing.faqCapabilitySources")}</li>
            <li>{t("landing.faqCapabilityReduce")}</li>
            <li>{t("landing.faqCapabilityAnalytics")}</li>
          </ul>
        </div>
        <LandingFaqWalkthrough className={styles.visual} />
      </section>
    </div>
  );
}
