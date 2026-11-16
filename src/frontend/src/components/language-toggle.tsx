"use client";

import { useLanguage } from "@/components/language-provider";
import { usePathname } from "next/navigation";

type Props = { floating?: boolean };

const workspaceRoots = [
  "/dashboard", "/sdk-guide", "/applications", "/offline", "/available-models", "/models", "/providers",
  "/model-pools", "/my-endpoints", "/requests", "/usage", "/billing", "/incidents", "/reports", "/policy-reviews",
];

function VietnameseFlag() {
  return <svg viewBox="0 0 24 16" aria-hidden="true"><rect width="24" height="16" rx="2" fill="#DA251D" /><path d="m12 3 1.45 3.05 3.36.4-2.46 2.32.66 3.3L12 10.4l-3.01 1.67.66-3.3L7.2 6.45l3.36-.4L12 3Z" fill="#FFDF00" /></svg>;
}

function EnglishFlag() {
  return <svg viewBox="0 0 24 16" aria-hidden="true"><rect width="24" height="16" rx="2" fill="#17356B" /><path d="m1 0 22 16M23 0 1 16" stroke="#fff" strokeWidth="4" /><path d="m1 0 22 16M23 0 1 16" stroke="#C8102E" strokeWidth="1.7" /><path d="M12 0v16M0 8h24" stroke="#fff" strokeWidth="6" /><path d="M12 0v16M0 8h24" stroke="#C8102E" strokeWidth="3" /></svg>;
}

export function LanguageToggle({ floating = false }: Props) {
  const { language, setLanguage, t } = useLanguage();
  const pathname = usePathname();

  const isWorkspacePath = workspaceRoots.some((root) => pathname === root || pathname.startsWith(`${root}/`));
  const isAccountAuthPath = pathname === "/forgot-password" || pathname === "/update-password";
  const isAuthToolbarPath = pathname === "/login" || pathname === "/sign-up" || isAccountAuthPath;
  if (floating && (pathname === "/" || isWorkspacePath || isAuthToolbarPath)) return null;

  return (
    <div className={`language-toggle--flags${floating ? " language-toggle--floating" : ""}`} role="group" aria-label={t("language.select")}>
      <button type="button" aria-label={t("language.selectVietnamese")} title={t("language.selectVietnamese")} aria-pressed={language === "vi"} onClick={() => setLanguage("vi")}>
        <VietnameseFlag />
      </button>
      <button type="button" aria-label={t("language.selectEnglish")} title={t("language.selectEnglish")} aria-pressed={language === "en"} onClick={() => setLanguage("en")}>
        <EnglishFlag />
      </button>
    </div>
  );
}

