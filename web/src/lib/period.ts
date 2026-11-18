/** Time filter of the map and the statistics. All instants are UTC. */
import type { Period } from "../api/types";

export type PeriodPreset = "7d" | "30d" | "90d" | "year" | "all" | "custom";

export const PERIOD_LABELS: Record<PeriodPreset, string> = {
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  "90d": "Last 90 days",
  year: "This year",
  all: "All data",
  custom: "Custom range",
};

const DAY_MS = 86_400_000;

/** First instant of a `YYYY-MM-DD` day and last instant of another, as ISO strings. */
export function customPeriod(startDay: string, endDay: string): Period {
  const valid = /^\d{4}-\d{2}-\d{2}$/;
  return {
    start: valid.test(startDay) ? `${startDay}T00:00:00Z` : null,
    end: valid.test(endDay) ? `${endDay}T23:59:59Z` : null,
  };
}

export function resolvePeriod(
  preset: PeriodPreset,
  now: Date,
  custom: { startDay: string; endDay: string } = { startDay: "", endDay: "" },
): Period {
  switch (preset) {
    case "7d":
    case "30d":
    case "90d": {
      const days = Number.parseInt(preset, 10);
      // Rounded down to the hour so that the tile URLs (and the browser cache) stay
      // stable while the page is open.
      const end = new Date(Math.floor(now.getTime() / 3_600_000) * 3_600_000 + 3_600_000);
      return { start: new Date(end.getTime() - days * DAY_MS).toISOString(), end: null };
    }
    case "year":
      return { start: `${now.getUTCFullYear()}-01-01T00:00:00.000Z`, end: null };
    case "custom":
      return customPeriod(custom.startDay, custom.endDay);
    case "all":
      return { start: null, end: null };
  }
}

/** `YYYY-MM-DD` of an ISO instant (UTC), or null. Used for the date-only statistics API. */
export function isoDay(instant: string | null): string | null {
  return instant ? instant.slice(0, 10) : null;
}
