"use client";

import type { CSSProperties } from "react";
import { useLanguage } from "@/components/language-provider";

type SkeletonProps = { label?: string };

function Block({ className = "" }: { className?: string }) {
  return <span className={`skeleton-block ${className}`} />;
}

function Heading() {
  return <section className="gateway-page-heading skeleton-page-heading" aria-hidden="true"><div><Block className="skeleton-eyebrow" /><Block className="skeleton-page-title" /><Block className="skeleton-page-copy" /></div><Block className="skeleton-page-action" /></section>;
}

function Table({ columns = 5, rows = 5 }: { columns?: number; rows?: number }) {
  return <section className="gateway-panel skeleton-table-panel" aria-hidden="true" style={{ "--skeleton-columns": columns } as CSSProperties}><div className="skeleton-table-head">{Array.from({ length: columns }, (_, index) => <Block className="skeleton-table-cell" key={index} />)}</div>{Array.from({ length: rows }, (_, row) => <div className="skeleton-table-line" key={row}>{Array.from({ length: columns }, (_, column) => <Block className="skeleton-table-cell" key={column} />)}</div>)}</section>;
}

function Filters({ count = 3 }: { count?: number }) {
  return <section className="filter-bar skeleton-filter-bar" aria-hidden="true">{Array.from({ length: count }, (_, index) => <div className="skeleton-filter" key={index}><Block className="skeleton-field-label" /><Block className="skeleton-field-control" /></div>)}</section>;
}

function Form({ fields = 6 }: { fields?: number }) {
  return <section className="gateway-panel form-panel skeleton-form-panel" aria-hidden="true"><div className="form-grid">{Array.from({ length: fields }, (_, index) => <div className={index === 0 || index === fields - 2 ? "skeleton-field full" : "skeleton-field"} key={index}><Block className="skeleton-field-label" /><Block className={index === fields - 2 ? "skeleton-textarea" : "skeleton-field-control"} /></div>)}</div><div className="form-actions"><Block className="skeleton-form-button" /><Block className="skeleton-form-button" /></div></section>;
}

function MetricGrid({ count = 4 }: { count?: number }) {
  return <section className="gateway-metric-grid skeleton-metric-grid" aria-hidden="true">{Array.from({ length: count }, (_, index) => <article className="gateway-metric" key={index}><Block className="skeleton-card-label" /><Block className="skeleton-card-value" /><Block className="skeleton-card-copy" /></article>)}</section>;
}

export function DashboardSkeleton({ label }: SkeletonProps) {
  const { t } = useLanguage();
  return <section className="gateway-page-skeleton" aria-busy="true" aria-live="polite"><Heading /><MetricGrid count={5} /><Table columns={2} rows={4} /><span className="sr-only">{label ?? t("skeleton.dashboard")}</span></section>;
}

export function ListPageSkeleton({ label, filters = 3, columns = 5 }: SkeletonProps & { filters?: number; columns?: number }) {
  const { t } = useLanguage();
  return <section className="gateway-page-skeleton" aria-busy="true" aria-live="polite"><Heading /><Filters count={filters} /><Table columns={columns} /><span className="sr-only">{label ?? t("skeleton.list")}</span></section>;
}

export function DetailSkeleton({ label }: SkeletonProps) {
  const { t } = useLanguage();
  return <section className="gateway-page-skeleton" aria-busy="true" aria-live="polite"><Heading /><div className="skeleton-tabs"><Block /><Block /><Block /></div><MetricGrid /><span className="sr-only">{label ?? t("skeleton.details")}</span></section>;
}

export function FormPageSkeleton({ label, fields = 6 }: SkeletonProps & { fields?: number }) {
  const { t } = useLanguage();
  return <section className="gateway-page-skeleton" aria-busy="true" aria-live="polite"><Heading /><Form fields={fields} /><span className="sr-only">{label ?? t("skeleton.form")}</span></section>;
}

export function BillingSkeleton({ label }: SkeletonProps) {
  const { t } = useLanguage();
  return <div className="billing-page"><section className="gateway-page-skeleton" aria-busy="true" aria-live="polite"><Heading /><section className="billing-overview" aria-hidden="true"><div className="billing-balance-card skeleton-billing-balance"><Block className="skeleton-card-label" /><Block className="skeleton-balance-value" /><Block className="skeleton-card-copy" /></div><MetricGrid count={3} /></section><div className="billing-detail-grid"><Table columns={5} rows={4} /><Form fields={2} /></div><span className="sr-only">{label ?? t("skeleton.billing")}</span></section></div>;
}

export function PlaygroundSkeleton({ label }: SkeletonProps) {
  const { t } = useLanguage();
  return <section className="gateway-page-skeleton" aria-busy="true" aria-live="polite"><Heading /><div className="skeleton-tabs"><Block /><Block /><Block /></div><div className="playground-grid" aria-hidden="true"><section className="gateway-panel composer skeleton-playground-panel"><Block className="skeleton-panel-title" /><Block className="skeleton-textarea skeleton-playground-textarea" /><Block className="skeleton-form-button" /></section><section className="gateway-panel response-panel skeleton-playground-panel"><Block className="skeleton-panel-title" /><Block className="skeleton-response-line" /><Block className="skeleton-response-line skeleton-response-line--short" /><Block className="skeleton-response-line" /></section></div><span className="sr-only">{label ?? t("skeleton.playground")}</span></section>;
}

export function ModelCatalogSkeleton({ label }: SkeletonProps) {
  const { t } = useLanguage();
  return <section className="gateway-page-skeleton" aria-busy="true" aria-live="polite"><Heading /><Filters /><section className="model-grid skeleton-model-grid" aria-hidden="true">{Array.from({ length: 6 }, (_, index) => <article className="model-card skeleton-model-card" key={index}><Block className="skeleton-model-code" /><Block className="skeleton-panel-title" /><Block className="skeleton-response-line" /><div className="skeleton-model-stats"><Block /><Block /><Block /></div></article>)}</section><span className="sr-only">{label ?? t("skeleton.models")}</span></section>;
}

export function PolicyReviewSkeleton({ label }: SkeletonProps) {
  const { t } = useLanguage();
  return <section className="gateway-page-skeleton" aria-busy="true" aria-live="polite"><Heading /><div className="review-layout" aria-hidden="true"><section className="gateway-panel review-queue skeleton-review-queue">{Array.from({ length: 5 }, (_, index) => <div className="skeleton-queue-row" key={index}><Block /><Block /><Block /></div>)}</section><section className="gateway-panel review-detail skeleton-review-detail"><Block className="skeleton-panel-title" /><Block className="skeleton-response-line" /><MetricGrid count={3} /><Block className="skeleton-textarea" /></section></div><span className="sr-only">{label ?? t("skeleton.policyReviews")}</span></section>;
}

export function GatewayPageSkeleton({ route }: { route: string }) {
  const { t } = useLanguage();
  if (route === "/dashboard") return <DashboardSkeleton />;
  if (route === "/billing") return <BillingSkeleton />;
  if (route.endsWith("/playground")) return <PlaygroundSkeleton />;
  if (route === "/policy-reviews") return <PolicyReviewSkeleton />;
  if (route === "/applications/new" || route.endsWith("/settings") || route === "/models/new" || route === "/providers" || route === "/model-pools/new" || route === "/incidents/new" || route.startsWith("/model-pools/")) return <FormPageSkeleton />;
  if (/^\/applications\/[^/]+$/.test(route)) return <DetailSkeleton />;
  if (route === "/usage") return <ListPageSkeleton label={t("skeleton.usage")} filters={7} columns={9} />;
  if (route === "/requests") return <ListPageSkeleton label={t("skeleton.requestHistory")} filters={4} columns={8} />;
  if (/^\/requests\/[^/]+$/.test(route)) return <DetailSkeleton label={t("skeleton.requestTrace")} />;
  if (route === "/available-models") return <ListPageSkeleton label={t("skeleton.models")} filters={3} columns={8} />;
  if (route === "/models") return <ListPageSkeleton label={t("skeleton.models")} filters={3} columns={8} />;
  if (route === "/incidents") return <ListPageSkeleton label={t("skeleton.incidents")} filters={3} columns={3} />;
  if (route === "/model-pools") return <ListPageSkeleton label={t("skeleton.modelPools")} filters={0} columns={6} />;
  return <ListPageSkeleton label={t("skeleton.applications")} filters={3} columns={5} />;
}
