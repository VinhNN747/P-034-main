const MAX_DECIMAL_PLACES = 10;

function decimalPlacesForScale(value: number): number {
  const absolute = Math.abs(value);
  if (!absolute || absolute >= 1) return 0;
  return Math.max(0, Math.ceil(-Math.log10(absolute)) + 1);
}

/**
 * Keeps small usage costs and small differences visible without filling normal
 * values with insignificant trailing zeroes.
 */
export function supportDecimalPlaces(values: Array<number | null>, unit: string): number {
  const finite = values.filter((value): value is number => value !== null && Number.isFinite(value));
  if (unit !== "USD") return 6;

  const unique = [...new Set(finite)].sort((left, right) => left - right);
  const positiveMagnitudes = unique.map(Math.abs).filter((value) => value > 0);
  const differences = unique.slice(1).map((value, index) => value - unique[index]).filter((value) => value > 0);
  const smallestMeaningful = Math.min(...positiveMagnitudes, ...differences);

  if (!Number.isFinite(smallestMeaningful)) return 2;
  return Math.min(MAX_DECIMAL_PLACES, Math.max(2, decimalPlacesForScale(smallestMeaningful)));
}

export function formatSupportNumber(value: number, values: Array<number | null>, unit: string): string {
  return new Intl.NumberFormat("vi-VN", {
    maximumFractionDigits: supportDecimalPlaces(values, unit),
  }).format(value);
}
