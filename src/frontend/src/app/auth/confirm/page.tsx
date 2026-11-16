"use client";

import { createBrowserClient } from "@supabase/ssr";
import { useEffect, useRef } from "react";

import { useLanguage } from "@/components/language-provider";
import { createAuthConfirmation } from "@/lib/auth-confirmation";

function finishConfirmation(path: string) {
  // Remove credentials from browser history before leaving the callback page.
  window.history.replaceState({}, "", "/auth/confirm");
  window.location.replace(path);
}

export default function AuthConfirmPage() {
  const { t } = useLanguage();
  const confirmation = useRef<ReturnType<typeof createAuthConfirmation> | null>(null);
  useEffect(() => {
    let active = true;
    try {
      if (!confirmation.current) {
        const supabase = createBrowserClient(
          process.env.NEXT_PUBLIC_SUPABASE_URL!,
          process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
          // Process the callback explicitly. Automatic URL detection uses
          // PKCE and rejects default recovery emails with an implicit session.
          { isSingleton: false, auth: { detectSessionInUrl: false } },
        );
        confirmation.current = createAuthConfirmation(supabase.auth, new URL(window.location.href));
      }
      void confirmation.current().then((path) => {
        if (active) finishConfirmation(path);
      });
    } catch {
      if (active) finishConfirmation("/auth/error");
    }
    return () => { active = false; };
  }, []);

  return (
    <main className="auth-page">
      <section className="auth-card" aria-live="polite">
        <p className="eyebrow">{t("auth.confirmEyebrow")}</p>
        <h1>{t("auth.confirmTitle")}</h1>
        <p className="auth-description">{t("auth.confirmDescription")}</p>
      </section>
    </main>
  );
}
