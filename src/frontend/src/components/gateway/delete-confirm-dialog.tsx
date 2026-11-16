"use client";

import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { useLanguage } from "@/components/language-provider";

export function DeleteConfirmDialog({
  resourceType,
  resourceName,
  description,
  pending,
  error,
  onClose,
  onConfirm,
}: {
  resourceType: string;
  resourceName: string;
  description: string;
  pending: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const { t } = useLanguage();
  const titleId = useId();
  const descriptionId = useId();
  const closeRef = useRef(onClose);
  const pendingRef = useRef(pending);
  const dialogRef = useRef<HTMLElement>(null);
  const cancelButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current = onClose;
    pendingRef.current = pending;
  }, [onClose, pending]);

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !pendingRef.current) closeRef.current();
      if (event.key === "Tab") {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
        );
        if (!focusable?.length) {
          event.preventDefault();
          dialogRef.current?.focus();
          return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKeyDown);
    document.body.style.overflow = "hidden";
    cancelButtonRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, []);

  if (typeof document === "undefined") return null;

  return createPortal(
    <div
      className="model-capability-dialog-scrim gateway-delete-dialog-scrim"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !pending) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className="model-capability-dialog gateway-delete-dialog"
        role="alertdialog"
        aria-modal="true"
        tabIndex={-1}
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
      >
        <header>
          <div>
            <span>{t("delete.permanent")}</span>
            <h2 id={titleId}>{t("delete.title", { resource: resourceType })}</h2>
            <p id={descriptionId}>{description}</p>
          </div>
          <button type="button" onClick={onClose} aria-label={t("delete.close", { resource: resourceType })} disabled={pending}>×</button>
        </header>
        <div className="gateway-delete-dialog-body">
          <strong>{resourceName}</strong>
          {error && <p className="gateway-delete-dialog-error" role="alert">{error}</p>}
        </div>
        <footer>
          <button ref={cancelButtonRef} type="button" className="gateway-button gateway-button--ghost" onClick={onClose} disabled={pending}>
            {t("common.cancel")}
          </button>
          <button type="button" className="gateway-button gateway-button--danger" onClick={onConfirm} disabled={pending}>
            {pending ? t("common.deleting") : t("delete.confirm")}
          </button>
        </footer>
      </section>
    </div>,
    document.querySelector(".gateway-shell") ?? document.body,
  );
}
