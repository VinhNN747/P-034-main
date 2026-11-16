"use client";

import { useEffect, useState } from "react";

import { useLanguage } from "@/components/language-provider";

type Theme = "light" | "dark";
type Props = { compact?: boolean };
const STORAGE_KEY = "ai20k:theme";

function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
}

export function ThemeToggle({ compact = false }: Props) {
  const [theme, setTheme] = useState<Theme>("light");
  const { t } = useLanguage();

  useEffect(() => {
    const savedTheme: Theme = window.localStorage.getItem(STORAGE_KEY) === "dark" ? "dark" : "light";
    setTheme(savedTheme);
    applyTheme(savedTheme);
  }, []);

  function toggleTheme() {
    const nextTheme: Theme = theme === "light" ? "dark" : "light";
    setTheme(nextTheme);
    applyTheme(nextTheme);
    window.localStorage.setItem(STORAGE_KEY, nextTheme);
  }

  const label = theme === "light" ? t("theme.switchToDark") : t("theme.switchToLight");

  return (
    <button
      className={`theme-toggle${compact ? " theme-toggle--compact" : ""}`}
      type="button"
      onClick={toggleTheme}
      aria-label={label}
      title={label}
      aria-pressed={theme === "dark"}
    >
      <span className="theme-toggle-icon" aria-hidden="true">
        {theme === "light"
          ? <svg viewBox="0 0 24 24" fill="none"><path d="M20.2 15.2A8.8 8.8 0 0 1 8.8 3.8 8.8 8.8 0 1 0 20.2 15.2Z" /></svg>
          : <svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="4" /><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42" /></svg>}
      </span>
      {!compact && <span className="theme-toggle-label">{theme === "light" ? t("theme.dark") : t("theme.light")}</span>}
    </button>
  );
}
