"use client";

import { useState } from "react";
import { useLanguage } from "@/components/language-provider";

export type FilterFieldType = "text" | "enum" | "number";
export type FilterOperator = "contains" | "equals" | "not_equals" | "greater_than" | "less_than";
export type FilterField = {
  key: string;
  label: string;
  type: FilterFieldType;
  options?: readonly { value: string; label: string }[];
};
export type FilterCondition = {
  id: string;
  field: string;
  operator: FilterOperator;
  value: string;
};

const operators: Record<FilterFieldType, readonly { value: FilterOperator; label: string }[]> = {
  text: [
    { value: "contains", label: "Contains" },
    { value: "equals", label: "Equals" },
    { value: "not_equals", label: "Not equal" },
  ],
  enum: [
    { value: "equals", label: "Equals" },
    { value: "not_equals", label: "Not equal" },
  ],
  number: [
    { value: "equals", label: "Equals" },
    { value: "greater_than", label: "Greater than" },
    { value: "less_than", label: "Less than" },
  ],
};

function defaultOperator(type: FilterFieldType): FilterOperator {
  return operators[type][0].value;
}

export function FilterBuilder({ fields, value, onChange }: { fields: readonly FilterField[]; value: FilterCondition[]; onChange: (next: FilterCondition[]) => void }) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(value.length > 0);
  const [draftField, setDraftField] = useState(fields[0]?.key ?? "");
  const selectedDraftField = fields.find((field) => field.key === draftField) ?? fields[0];

  function addFilter() {
    if (!selectedDraftField) return;
    onChange([...value, { id: `${selectedDraftField.key}-${Date.now()}`, field: selectedDraftField.key, operator: defaultOperator(selectedDraftField.type), value: "" }]);
    setDraftField(fields.find((field) => !value.some((item) => item.field === field.key))?.key ?? fields[0]?.key ?? "");
    setOpen(true);
  }

  function updateFilter(id: string, patch: Partial<FilterCondition>) {
    onChange(value.map((item) => item.id === id ? { ...item, ...patch } : item));
  }

  function changeField(id: string, fieldKey: string) {
    const field = fields.find((item) => item.key === fieldKey) ?? fields[0];
    if (!field) return;
    updateFilter(id, { field: field.key, operator: defaultOperator(field.type), value: "" });
  }

  function operatorLabel(operator: FilterOperator) {
    if (operator === "contains") return t("filter.contains");
    if (operator === "equals") return t("filter.equals");
    if (operator === "not_equals") return t("filter.notEquals");
    if (operator === "greater_than") return t("filter.greaterThan");
    return t("filter.lessThan");
  }

  return (
    <section className="filter-builder" aria-label={t("filter.filters")}>
      {value.map((item) => {
        const field = fields.find((candidate) => candidate.key === item.field) ?? fields[0];
        if (!field) return null;
        const fieldOperators = operators[field.type];
        return (
          <div className="filter-builder-row" key={item.id}>
            <select aria-label={t("filter.field")} value={field.key} onChange={(event) => changeField(item.id, event.target.value)}>
              {fields.map((candidate) => <option value={candidate.key} key={candidate.key}>{candidate.label}</option>)}
            </select>
            <select aria-label={t("filter.operator")} value={item.operator} onChange={(event) => updateFilter(item.id, { operator: event.target.value as FilterOperator })}>
              {fieldOperators.map((operator) => <option value={operator.value} key={operator.value}>{operatorLabel(operator.value)}</option>)}
            </select>
            {field.type === "enum" && field.options ? (
              <select aria-label={`${field.label} value`} value={item.value} onChange={(event) => updateFilter(item.id, { value: event.target.value })}>
                <option value="">{t("filter.selectValue")}</option>
                {field.options.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
            ) : (
              <input aria-label={`${field.label} value`} type={field.type === "number" ? "number" : "text"} value={item.value} onChange={(event) => updateFilter(item.id, { value: event.target.value })} placeholder={t("filter.enterValue", { field: field.label.toLowerCase() })} />
            )}
            <button type="button" className="filter-builder-remove" onClick={() => onChange(value.filter((candidate) => candidate.id !== item.id))} aria-label={t("filter.remove", { field: field.label })}>×</button>
          </div>
        );
      })}
      {(open || value.length > 0) && <div className="filter-builder-add-row">
        <select aria-label={t("filter.newField")} value={draftField} onChange={(event) => setDraftField(event.target.value)}>
          {fields.map((field) => <option value={field.key} key={field.key}>{field.label}</option>)}
        </select>
        <button type="button" className="filter-builder-add" onClick={addFilter}>+ {t("filter.add")}</button>
      </div>}
      {!open && value.length === 0 && <button type="button" className="filter-builder-trigger" onClick={() => setOpen(true)}>+ {t("filter.add")}</button>}
    </section>
  );
}
