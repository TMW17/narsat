import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ActivityDay, QualityCheck } from "../api/types";
import { BarChart, fillDays, isMonthly, niceCeiling } from "./charts";
import { detectionsPerDay } from "./EventPanel";
import { Legend } from "./Legend";
import { groupChecks, verdict } from "./QualityView";
import { dailyTotals } from "./StatsView";

describe("BarChart", () => {
  it("draws one bar per value with an accessible title", () => {
    const html = renderToStaticMarkup(
      <BarChart
        title="Detections per day"
        unit="detections"
        bars={[
          { label: "2023-07-22", value: 2 },
          { label: "2023-07-23", value: 4 },
          { label: "2023-07-24", value: 0 },
        ]}
      />,
    );
    expect(html.match(/<rect/g)).toHaveLength(3);
    expect(html).toContain(">2.5</text>"); // the middle grid line of a 0-5 axis
    expect(html).toContain('aria-label="Detections per day"');
    expect(html).toContain("2023-07-23: 4 detections");
  });

  it("says so when there is nothing to draw", () => {
    expect(renderToStaticMarkup(<BarChart title="t" unit="u" bars={[]} />)).toContain("No data");
  });

  it("rounds the axis up to 1, 2 or 5 times a power of ten", () => {
    expect([0, 1, 3, 7, 18, 480, 5000].map(niceCeiling)).toEqual([1, 1, 5, 10, 20, 500, 5000]);
  });
});

describe("aggregation helpers", () => {
  it("fills the days without detections", () => {
    expect(
      detectionsPerDay(["2023-07-22T12:00:00Z", "2023-07-22T18:00:00Z", "2023-07-24T06:00:00+00:00"]),
    ).toEqual([
      { label: "2023-07-22", value: 2 },
      { label: "2023-07-23", value: 0 },
      { label: "2023-07-24", value: 1 },
    ]);
    expect(detectionsPerDay([])).toEqual([]);
  });

  it("counts by UTC day whatever the offset of the timestamp", () => {
    // 00:30 in Algiers (UTC+1) is still the previous day in UTC.
    expect(detectionsPerDay(["2023-07-23T00:30:00+01:00", "not a date"])).toEqual([
      { label: "2023-07-22", value: 1 },
    ]);
  });

  it("sums the countries of a day", () => {
    const day = (activity_date: string, country_iso3: string, n_hotspots: number): ActivityDay => ({
      activity_date,
      country_iso3,
      n_hotspots,
      n_events: 1,
      frp_sum_mw: null,
    });
    expect(dailyTotals([day("2023-07-23", "TUN", 3), day("2023-07-21", "DZA", 2), day("2023-07-23", "DZA", 4)])).toEqual([
      { label: "2023-07-21", value: 2 },
      { label: "2023-07-22", value: 0 },
      { label: "2023-07-23", value: 7 },
    ]);
  });

  it("groups long spans by month so the chart stays readable", () => {
    const values = new Map([
      ["2022-01-15", 3],
      ["2022-01-20", 1],
      ["2023-03-02", 5],
    ]);
    const bars = fillDays(values);
    expect(bars).toHaveLength(15); // January 2022 to March 2023
    expect(bars[0]).toEqual({ label: "2022-01", value: 4 });
    expect(bars[1]).toEqual({ label: "2022-02", value: 0 });
    expect(bars[14]).toEqual({ label: "2023-03", value: 5 });
    expect(isMonthly(bars)).toBe(true);
    expect(isMonthly(fillDays(new Map([["2023-07-24", 2]])))).toBe(false);
    expect(isMonthly([])).toBe(false);
  });

  it("groups quality checks by suite with blocking failures first", () => {
    const check = (suite: string, check_name: string, status: QualityCheck["status"], severity: QualityCheck["severity"]): QualityCheck => ({
      suite,
      check_name,
      status,
      severity,
      observed: null,
      detail: null,
      checked_at: "2026-08-15T02:31:00Z",
    });
    const groups = groupChecks([
      check("silver.hotspots", "b_passes", "pass", "error"),
      check("silver.hotspots", "c_warns", "warn", "warn"),
      check("gold.burned_areas", "area_positive", "pass", "error"),
      check("silver.hotspots", "a_blocks", "fail", "error"),
    ]);
    expect(groups.map(([suite]) => suite)).toEqual(["gold.burned_areas", "silver.hotspots"]);
    expect(groups[1]?.[1].map((item) => item.check_name)).toEqual(["a_blocks", "c_warns", "b_passes"]);
    expect(groups[1]?.[1].map((item) => verdict(item).text)).toEqual(["Fail (blocking)", "Warning", "Pass"]);
  });
});

describe("Legend", () => {
  it("reflects the layer switches and disables the method filter with its layer", () => {
    const html = renderToStaticMarkup(
      <Legend
        visible={{ hotspots: true, events: false, burned_areas: false }}
        method="model"
        onToggle={() => undefined}
        onMethodChange={() => undefined}
      />,
    );
    expect(html.match(/type="checkbox"/g)).toHaveLength(3);
    expect(html.match(/checked=""/g)).toHaveLength(1);
    expect(html).toMatch(/<select[^>]*disabled=""/);
    expect(html).toContain('<option value="model" selected="">Model only</option>');
  });
});
