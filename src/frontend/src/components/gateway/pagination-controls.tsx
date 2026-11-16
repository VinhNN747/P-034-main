"use client";

import { useState } from "react";
import { useLanguage } from "@/components/language-provider";

export const TABLE_PAGE_SIZE = 10;

export function useClientTablePagination<T>(items: readonly T[], resetKey?: string | number | null) {
  const [pagination, setPagination] = useState({ page: 1, resetKey });
  const pageCount = Math.max(1, Math.ceil(items.length / TABLE_PAGE_SIZE));
  const page = pagination.resetKey === resetKey ? Math.min(pagination.page, pageCount) : 1;
  const firstIndex = (page - 1) * TABLE_PAGE_SIZE;

  return {
    page,
    pageItems: items.slice(firstIndex, firstIndex + TABLE_PAGE_SIZE),
    setPage: (nextPage: number) => setPagination({
      page: Math.max(1, Math.min(nextPage, pageCount)),
      resetKey,
    }),
  };
}

type PaginationControlsProps = {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  label?: string;
};

export function PaginationControls({
  page,
  pageSize,
  total,
  onPageChange,
  label = "Pagination",
}: PaginationControlsProps) {
  const { t, language } = useLanguage();
  if (total === 0) return null;
  const first = (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, total);
  const hasPrevious = page > 1;
  const hasNext = last < total;

  const localizedLabel = language === "vi" ? ({
    "API keys pagination": "Phân trang API keys",
    "Billing transactions pagination": t("billing.pagination"),
    "Incidents pagination": t("incidents.pagination"),
    "Model pools pagination": "Phân trang model pools",
    "Available models pagination": t("models.availablePagination"),
    "Models pagination": t("models.pagination"),
    "Applications pagination": t("applications.pagination"),
    "Comparison cohorts pagination": t("usage.comparisonPagination"),
    "Usage pagination": t("usage.pagination"),
    "Comparison branches pagination": "Phân trang comparison branches",
    "Provider connections pagination": t("providers.pagination"),
    "Request history pagination": t("requests.pagination"),
    "Policy reviews pagination": "Phân trang policy reviews",
  } as Record<string, string>)[label ?? ""] ?? label : (label || t("table.pagination"));

  return (
    <nav className="gateway-pagination" aria-label={localizedLabel || t("table.pagination")}>
      <span aria-live="polite">{t("table.showing", { first, last, total })}</span>
      <div>
        <button type="button" className="gateway-button gateway-button--ghost" disabled={!hasPrevious} onClick={() => onPageChange(page - 1)}>
          {t("table.previous")}
        </button>
        <span>{t("table.page", { page })}</span>
        <button type="button" className="gateway-button gateway-button--ghost" disabled={!hasNext} onClick={() => onPageChange(page + 1)}>
          {t("table.next")}
        </button>
      </div>
    </nav>
  );
}
