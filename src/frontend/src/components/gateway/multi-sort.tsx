"use client";

import { useLanguage } from "@/components/language-provider";

export type SortDirection = "asc" | "desc";
export type SortCriterion<Field extends string> = { field: Field; dir: SortDirection };

export function toggleSortCriterion<Field extends string>(criteria: SortCriterion<Field>[], field: Field): SortCriterion<Field>[] {
  const index = criteria.findIndex((criterion) => criterion.field === field);
  if (index === -1) return [...criteria, { field, dir: "asc" }];
  if (criteria[index].dir === "asc") {
    return criteria.map((criterion, criterionIndex) => criterionIndex === index ? { ...criterion, dir: "desc" } : criterion);
  }
  return criteria.filter((criterion) => criterion.field !== field);
}

export function parseSortCriteria<Field extends string>(sortBy: string | null, sortDir: string | null, allowedFields: readonly Field[]): SortCriterion<Field>[] {
  const allowed = new Set<string>(allowedFields);
  const directions = (sortDir ?? "").split(",");
  const fields = (sortBy ?? "").split(",").filter((field): field is Field => allowed.has(field));
  return [...new Set(fields)].map((field, index) => ({ field, dir: directions[index] === "desc" ? "desc" : "asc" }));
}

export function sortQueryParams<Field extends string>(criteria: SortCriterion<Field>[]): { sort_by?: Field[]; sort_dir?: SortDirection[] } {
  return criteria.length > 0 ? { sort_by: criteria.map((criterion) => criterion.field), sort_dir: criteria.map((criterion) => criterion.dir) } : {};
}

export function SortableTableHeader<Field extends string>({ field, label, criteria, onToggle }: { field: Field; label: string; criteria: SortCriterion<Field>[]; onToggle: (field: Field) => void }) {
  const { t } = useLanguage();
  const index = criteria.findIndex((criterion) => criterion.field === field);
  const dir = index === -1 ? null : criteria[index].dir;
  const indicator = dir === "asc" ? `↑${index + 1}` : dir === "desc" ? `↓${index + 1}` : "↕";
  const direction = dir === "asc" ? t("table.sortAscending") : dir === "desc" ? t("table.sortDescending") : t("table.sortNotSorted");
  const ariaDirection = dir === "asc" ? "ascending" : dir === "desc" ? "descending" : "none";
  const priority = index === -1 ? "" : t("table.sortPriority", { priority: index + 1 });

  return <th aria-sort={index === 0 ? ariaDirection : "none"} data-sort-active={index !== -1 || undefined}>
    <button type="button" className={`table-sort${index === -1 ? "" : " is-sorted"}`} onClick={() => onToggle(field)} aria-label={`${label}, ${direction}${priority}`}>
      <span>{label}</span><span className="table-sort-indicator" aria-hidden="true">{indicator}</span>
    </button>
  </th>;
}

export function SortPrioritySummary<Field extends string>({ criteria, labels }: { criteria: SortCriterion<Field>[]; labels: Record<Field, string> }) {
  const { t } = useLanguage();
  if (criteria.length === 0) return null;
  return <p className="gateway-table-sort-summary" aria-live="polite">
    {t("table.sortSummary", { items: criteria.map((criterion, index) => `${index + 1}. ${labels[criterion.field]} ${criterion.dir === "asc" ? "↑" : "↓"}`).join(" · ") })}
  </p>;
}
