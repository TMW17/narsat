/** Number and date formatting. Dates are shown in UTC, the time base of the satellite data. */

const integer = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 0 });
const oneDecimal = new Intl.NumberFormat("en-GB", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const dateFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: "UTC",
  day: "2-digit",
  month: "short",
  year: "numeric",
});
const dateTimeFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: "UTC",
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export const MISSING = "–";

export function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? MISSING : integer.format(value);
}

/** Hectares: one decimal below 100 ha, none above. */
export function formatHa(value: number | null | undefined): string {
  if (value === null || value === undefined) return MISSING;
  return `${Math.abs(value) < 100 ? oneDecimal.format(value) : integer.format(value)} ha`;
}

export function formatMw(value: number | null | undefined): string {
  return value === null || value === undefined ? MISSING : `${oneDecimal.format(value)} MW`;
}

/** A ratio in [0, 1] as a percentage. */
export function formatPercent(value: number | null | undefined): string {
  return value === null || value === undefined ? MISSING : `${integer.format(value * 100)} %`;
}

/** Agreement scores (IoU, precision, recall) with two decimals. */
export function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined ? MISSING : value.toFixed(2);
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return MISSING;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? MISSING : dateFormat.format(date);
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return MISSING;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? MISSING : `${dateTimeFormat.format(date)} UTC`;
}

export function formatDuration(hours: number | null | undefined): string {
  if (hours === null || hours === undefined) return MISSING;
  // First and last detection in the same satellite pass.
  if (hours === 0) return "one pass";
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  if (hours < 48) return `${oneDecimal.format(hours)} h`;
  return `${oneDecimal.format(hours / 24)} days`;
}

/** "3 h ago", "2 days ago": how old a timestamp is relative to `now`. */
export function formatAge(iso: string | null | undefined, now: Date): string {
  if (!iso) return MISSING;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return MISSING;
  const minutes = Math.max(0, Math.round((now.getTime() - then) / 60_000));
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} h ago`;
  return `${Math.round(minutes / 1440)} days ago`;
}

const STATUS_LABELS: Record<string, string> = {
  active: "Active",
  cooling: "Cooling",
  closed: "Closed",
  not_ready: "Not ready",
  pending: "Queued",
  imagery_pending: "Waiting for imagery",
  mapped: "Mapped",
  failed: "Failed",
  skipped: "Skipped",
  auto: "Not reviewed",
  accepted: "Accepted",
  rejected: "Rejected",
  dnbr: "dNBR (spectral index)",
  model: "Foundation model",
};

export function label(code: string | null | undefined): string {
  if (!code) return MISSING;
  return STATUS_LABELS[code] ?? code;
}
