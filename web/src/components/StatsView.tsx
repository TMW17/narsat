import { useMemo, useState } from "react";

import { api } from "../api/client";
import type { ActivityDay, Method, Period } from "../api/types";
import {
  formatCount,
  formatDate,
  formatDateTime,
  formatDuration,
  formatHa,
  formatScore,
  label,
} from "../lib/format";
import { isoDay } from "../lib/period";
import { useAsync } from "../lib/useAsync";
import { BarChart, fillDays, isMonthly, type Bar } from "./charts";

interface StatsViewProps {
  period: Period;
  onOpenEvent: (eventId: string) => void;
}

/** One bar per day (the API returns one row per day and country); quiet days are zero.
 * Periods longer than 400 days are grouped by month (see `fillDays`). */
export function dailyTotals(rows: ActivityDay[]): Bar[] {
  const totals = new Map<string, number>();
  for (const row of rows) {
    totals.set(row.activity_date, (totals.get(row.activity_date) ?? 0) + row.n_hotspots);
  }
  return fillDays(totals);
}

function Card({ title, value, hint }: { title: string; value: string; hint?: string }) {
  return (
    <div className="card">
      <p className="card-title">{title}</p>
      <p className="card-value">{value}</p>
      {hint && <p className="card-hint">{hint}</p>}
    </div>
  );
}

export function StatsView({ period, onOpenEvent }: StatsViewProps) {
  const [method, setMethod] = useState<Method | "">("");
  const [year, setYear] = useState<number | "">("");
  const start = isoDay(period.start);
  const end = isoDay(period.end);

  const summary = useAsync("summary", (signal) => api.summary(signal));
  const activity = useAsync(`activity:${start}:${end}`, (signal) =>
    api.activity({ start, end }, signal),
  );
  const burned = useAsync(`burned:${year}:${method}`, (signal) =>
    api.burnedArea({ year: year === "" ? null : year, method: method === "" ? null : method }, signal),
  );
  const events = useAsync("events", (signal) => api.eventSummaries(25, signal));

  const bars = useMemo(() => dailyTotals(activity.data ?? []), [activity.data]);
  const bin = isMonthly(bars) ? "month" : "day";
  const years = useMemo(() => {
    const first = summary.data?.first_acquisition;
    const last = summary.data?.last_acquisition;
    if (!first || !last) return [];
    const result: number[] = [];
    for (let y = new Date(last).getUTCFullYear(); y >= new Date(first).getUTCFullYear(); y -= 1) {
      result.push(y);
    }
    return result;
  }, [summary.data]);

  return (
    <div className="page">
      <section aria-labelledby="summary-heading">
        <h2 id="summary-heading">Overview</h2>
        {summary.error && <p className="error">{summary.error}</p>}
        {summary.data && (
          <>
            <div className="cards">
              <Card
                title="Fire detections"
                value={formatCount(summary.data.hotspots)}
                hint={`${formatCount(summary.data.static_hotspots)} more from static heat sources, excluded`}
              />
              <Card
                title="Fire events"
                value={formatCount(summary.data.events)}
                hint={`${formatCount(summary.data.active_events)} active now`}
              />
              <Card title="Events mapped" value={formatCount(summary.data.mapped_events)} />
              <Card
                title="Burned area, dNBR"
                value={formatHa(summary.data.burned_area_ha.dnbr ?? null)}
              />
              <Card
                title="Burned area, model"
                value={formatHa(summary.data.burned_area_ha.model ?? null)}
              />
            </div>
            <p className="muted">
              Data from {formatDate(summary.data.first_acquisition)} to{" "}
              {formatDateTime(summary.data.last_acquisition)}. The two burned-area figures are
              independent estimates of the same fires, not quantities to add up.
            </p>
          </>
        )}
      </section>

      <section aria-labelledby="activity-heading">
        <h2 id="activity-heading">Fire detections per {bin}</h2>
        <p className="muted">
          For the period selected above (UTC {bin}s
          {bin === "month" ? "; periods longer than 400 days are shown by month" : ""}). Updated
          by the nightly lakehouse refresh.
        </p>
        {activity.error ? (
          <p className="error">{activity.error}</p>
        ) : (
          <BarChart bars={bars} title={`Satellite fire detections per ${bin}`} unit="detections" />
        )}
      </section>

      <section aria-labelledby="burned-heading">
        <h2 id="burned-heading">Burned area by region</h2>
        <div className="filters">
          <label>
            Year
            <select
              value={year}
              onChange={(change) => setYear(change.target.value ? Number(change.target.value) : "")}
            >
              <option value="">All years</option>
              {years.map((y) => (
                <option key={y} value={y}>
                  {y}
                </option>
              ))}
            </select>
          </label>
          <label>
            Method
            <select value={method} onChange={(change) => setMethod(change.target.value as Method | "")}>
              <option value="">Both methods</option>
              <option value="dnbr">{label("dnbr")}</option>
              <option value="model">{label("model")}</option>
            </select>
          </label>
        </div>
        {burned.error && <p className="error">{burned.error}</p>}
        {burned.data && burned.data.length === 0 && <p className="muted">No mapped fires yet.</p>}
        {burned.data && burned.data.length > 0 && (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Year</th>
                  <th scope="col">Country</th>
                  <th scope="col">Region</th>
                  <th scope="col">Method</th>
                  <th scope="col" className="num">Fires</th>
                  <th scope="col" className="num">Burned area</th>
                </tr>
              </thead>
              <tbody>
                {burned.data.map((row) => (
                  <tr key={`${row.fire_year}:${row.country_iso3}:${row.admin1_name}:${row.method}`}>
                    <td>{row.fire_year}</td>
                    <td>{row.country_iso3}</td>
                    <td>{row.admin1_name}</td>
                    <td>{label(row.method)}</td>
                    <td className="num">{formatCount(row.n_events)}</td>
                    <td className="num">{formatHa(row.area_ha)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section aria-labelledby="events-heading">
        <h2 id="events-heading">Largest fires</h2>
        <p className="muted">
          Ranked by the dNBR area, the more accurate method against an independent reference.
          IoU is the overlap between the two mapping methods (1 = identical outlines).
        </p>
        {events.error && <p className="error">{events.error}</p>}
        {events.data && events.data.length === 0 && <p className="muted">No events yet.</p>}
        {events.data && events.data.length > 0 && (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Region</th>
                  <th scope="col">First detection</th>
                  <th scope="col" className="num">Duration</th>
                  <th scope="col" className="num">Detections</th>
                  <th scope="col" className="num">dNBR</th>
                  <th scope="col" className="num">Model</th>
                  <th scope="col" className="num">IoU</th>
                  <th scope="col">
                    <span className="visually-hidden">Open</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {events.data.map((row) => (
                  <tr key={row.event_id}>
                    <td>
                      {row.admin1_name}, {row.country_iso3}
                    </td>
                    <td>{formatDate(row.first_seen)}</td>
                    <td className="num">{formatDuration(row.duration_hours)}</td>
                    <td className="num">{formatCount(row.n_hotspots)}</td>
                    <td className="num">{formatHa(row.area_dnbr_ha)}</td>
                    <td className="num">{formatHa(row.area_model_ha)}</td>
                    <td className="num">{formatScore(row.method_iou)}</td>
                    <td>
                      <button type="button" className="quiet" onClick={() => onOpenEvent(row.event_id)}>
                        Show on map
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
