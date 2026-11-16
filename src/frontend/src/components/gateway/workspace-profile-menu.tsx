"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { LogoutButton } from "@/components/logout-button";
import { useLanguage } from "@/components/language-provider";

export function WorkspaceProfileMenu({ isUser }: { isUser: boolean }) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function closeOnPointerDown(event: PointerEvent) {
      if (event.target instanceof Node && !rootRef.current?.contains(event.target)) setOpen(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", closeOnPointerDown);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnPointerDown);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div className="workspace-profile" ref={rootRef}>
      <button
        type="button"
        className="workspace-profile-trigger"
        aria-label={t("profile.label")}
        aria-expanded={open}
        aria-controls="workspace-profile-menu"
        onClick={() => setOpen((value) => !value)}
      >
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="8" r="3.4" /><path d="M4.7 20a7.3 7.3 0 0 1 14.6 0" /></svg>
        <span>{t("profile.label")}</span>
        <svg className="workspace-profile-chevron" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
      </button>
      {open && <div className="workspace-profile-menu" id="workspace-profile-menu" aria-label={t("profile.label")}>
        <Link href="/update-password" onClick={() => setOpen(false)}>{t("profile.changePassword")}</Link>
        {isUser && <>
          <Link href="/reports/new" onClick={() => setOpen(false)}>{t("profile.reportIssue")}</Link>
          <Link href="/reports" onClick={() => setOpen(false)}>{t("profile.reportHistory")}</Link>
        </>}
        <div className="workspace-profile-divider" />
        <LogoutButton compact />
      </div>}
    </div>
  );
}
