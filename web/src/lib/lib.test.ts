import { describe, expect, it } from "vitest";

import type { RasterAsset } from "../api/types";
import {
  formatAge,
  formatCount,
  formatDate,
  formatDateTime,
  formatDuration,
  formatHa,
  formatPercent,
  formatScore,
  label,
  MISSING,
} from "./format";
import { DATA_LAYERS, LAYER_GROUPS, CLICK_TARGETS, overlayChoices, paddedBounds } from "./layers";
import { customPeriod, isoDay, resolvePeriod } from "./period";
import { isLive, sessionFromToken } from "./session";

describe("period", () => {
  const now = new Date("2026-08-15T10:42:17Z");

  it("resolves relative presets against the next full hour", () => {
    expect(resolvePeriod("7d", now)).toEqual({ start: "2026-08-08T11:00:00.000Z", end: null });
    expect(resolvePeriod("90d", now).start).toBe("2026-05-17T11:00:00.000Z");
  });

  it("gives the same answer for the whole hour, so tile URLs stay cacheable", () => {
    const later = new Date("2026-08-15T10:59:59Z");
    expect(resolvePeriod("30d", later)).toEqual(resolvePeriod("30d", now));
  });

  it("starts the year in UTC", () => {
    expect(resolvePeriod("year", new Date("2026-01-01T00:30:00+01:00"))).toEqual({
      start: "2025-01-01T00:00:00.000Z",
      end: null,
    });
  });

  it("covers both custom days entirely and ignores malformed input", () => {
    expect(customPeriod("2023-07-22", "2023-07-24")).toEqual({
      start: "2023-07-22T00:00:00Z",
      end: "2023-07-24T23:59:59Z",
    });
    expect(customPeriod("22/07/2023", "")).toEqual({ start: null, end: null });
    expect(resolvePeriod("all", now)).toEqual({ start: null, end: null });
  });

  it("extracts the day for the date-only endpoints", () => {
    expect(isoDay("2023-07-22T00:00:00Z")).toBe("2023-07-22");
    expect(isoDay(null)).toBeNull();
  });
});

describe("format", () => {
  it("formats areas with a precision that matches their size", () => {
    expect(formatHa(12.345)).toBe("12.3 ha");
    expect(formatHa(25432.6)).toBe("25,433 ha");
    expect(formatHa(null)).toBe(MISSING);
  });

  it("shows instants in UTC whatever the browser time zone", () => {
    expect(formatDateTime("2023-07-22T23:30:00+00:00")).toBe("22 Jul 2023, 23:30 UTC");
    expect(formatDate("2023-07-22T23:30:00Z")).toBe("22 Jul 2023");
    expect(formatDate("not a date")).toBe(MISSING);
  });

  it("formats counts, ratios, scores and durations", () => {
    expect(formatCount(1234567)).toBe("1,234,567");
    expect(formatPercent(0.974)).toBe("97 %");
    expect(formatScore(0.76562)).toBe("0.77");
    expect(formatScore(null)).toBe(MISSING);
    expect(formatDuration(0)).toBe("one pass");
    expect(formatDuration(0.5)).toBe("30 min");
    expect(formatDuration(42)).toBe("42.0 h");
    expect(formatDuration(120)).toBe("5.0 days");
  });

  it("describes how old a timestamp is", () => {
    const now = new Date("2026-08-15T12:00:00Z");
    expect(formatAge("2026-08-15T11:40:00Z", now)).toBe("20 min ago");
    expect(formatAge("2026-08-15T03:00:00Z", now)).toBe("9 h ago");
    expect(formatAge("2026-08-10T12:00:00Z", now)).toBe("5 days ago");
    expect(formatAge(null, now)).toBe(MISSING);
  });

  it("labels status codes and passes unknown ones through", () => {
    expect(label("imagery_pending")).toBe("Waiting for imagery");
    expect(label("something_new")).toBe("something_new");
  });
});

describe("layers", () => {
  const raster = (kind: RasterAsset["kind"], method: RasterAsset["method"], tiles: Record<string, string>): RasterAsset => ({
    kind,
    method,
    uri: `s3://narsat/assets/${kind}.tif`,
    epsg: 32631,
    width: 10,
    height: 10,
    bands: [],
    created_at: "2023-08-01T00:00:00Z",
    bounds: [4.55, 36.7, 4.62, 36.76],
    tiles,
  });

  it("lists raster overlays in reading order with distinct keys", () => {
    const choices = overlayChoices([
      raster("mask", "dnbr", { mask: "/t/mask" }),
      raster("dnbr", null, { dnbr: "/t/dnbr" }),
      raster("pre", null, { true_color: "/t/pre-rgb", swir_false_color: "/t/pre-swir" }),
      raster("post", null, { true_color: "/t/post-rgb", swir_false_color: "/t/post-swir" }),
      raster("mask", "model", { mask: "/t/mask-model" }),
    ]);
    expect(choices.map((choice) => choice.label)).toEqual([
      "Post-fire image - true colour",
      "Post-fire image - SWIR false colour",
      "Pre-fire image - true colour",
      "Pre-fire image - SWIR false colour",
      "Burn severity index (dNBR)",
      "Burn mask (dNBR)",
      "Burn mask (model)",
    ]);
    expect(new Set(choices.map((choice) => choice.key)).size).toBe(choices.length);
    expect(choices.every((choice) => choice.bounds.join() === "4.55,36.7,4.62,36.76")).toBe(true);
  });

  it("pads the event bounding box", () => {
    expect(paddedBounds({ bbox: [4.5, 36.7, 4.7, 36.8] }, 0.1)).toEqual([4.4, 36.6, 4.8, 36.9].map((v) => expect.closeTo(v, 6)));
    expect(paddedBounds({ bbox: null })).toBeNull();
  });

  it("only references layers that exist", () => {
    const ids = new Set(DATA_LAYERS.map((layer) => layer.id));
    for (const group of Object.values(LAYER_GROUPS)) for (const id of group) expect(ids.has(id)).toBe(true);
    for (const target of CLICK_TARGETS) expect(ids.has(target.layer)).toBe(true);
  });
});

describe("session", () => {
  it("expires slightly before the token does", () => {
    const session = sessionFromToken(
      "analyst@example.org",
      { access_token: "t", token_type: "bearer", expires_in: 3600, role: "analyst" },
      1_000_000,
    );
    expect(session.expiresAt).toBe(1_000_000 + 3_570_000);
    expect(isLive(session, 1_000_000)).toBe(true);
    expect(isLive(session, session.expiresAt)).toBe(false);
    expect(isLive(null, 0)).toBe(false);
  });
});
