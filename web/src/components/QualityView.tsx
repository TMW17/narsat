import { useMemo } from "react";

import { api } from "../api/client";
import type { QualityCheck } from "../api/types";
import { formatAge, formatCount, formatDateTime } from "../lib/format";
import { useAsync } from "../lib/useAsync";

/** Checks grouped by suite, failures first inside each group. */
export function groupChecks(checks: QualityCheck[]): [string, QualityCheck[]][] {
  const groups = new Map<string, QualityCheck[]>();
  for (const check of checks) {
    const group = groups.get(check.suite) ?? [];
    group.push(check);
    groups.set(check.suite, group);
  }
  const rank = (check: QualityCheck) =>
    check.status === "pass" ? 2 : check.status === "fail" && check.severity === "error" ? 0 : 1;
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([suite, items]) => [
      suite,
      [...items].sort((a, b) => rank(a) - rank(b) || a.check_name.localeCompare(b.check_name)),
    ]);
}

export function verdict(check: QualityCheck): { text: string; tone: string } {
  if (check.status === "pass") return { text: "Pass", tone: "pass" };
  if (check.status === "fail" && check.severity === "error") {
    return { text: "Fail (blocking)", tone: "fail" };
  }
  return { text: "Warning", tone: "warn" };
}

function observed(check: QualityCheck): string {
  if (check.detail) return check.detail;
  if (!check.observed) return "";
  return Object.entries(check.observed)
    .map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`)
    .join(", ");
}

export function QualityView() {
  const freshness = useAsync("freshness", (signal) => api.freshness(signal));
  const checks = useAsync("checks", (signal) => api.checks(signal));
  const groups = useMemo(() => groupChecks(checks.data ?? []), [checks.data]);
  const now = new Date();

  return (
    <div className="page">
      <section aria-labelledby="freshness-heading">
        <h2 id="freshness-heading">Freshness</h2>
        {freshness.error && <p className="error">{freshness.error}</p>}
        {freshness.data && (
          <div className="cards">
            <div className="card">
              <p className="card-title">Latest satellite detection</p>
              <p className="card-value">{formatAge(freshness.data.last_acquisition, now)}</p>
              <p className="card-hint">{formatDateTime(freshness.data.last_acquisition)}</p>
            </div>
            <div className="card">
              <p className="card-title">Latest ingestion</p>
              <p className="card-value">{formatAge(freshness.data.last_ingestion, now)}</p>
              <p className="card-hint">
                {formatCount(freshness.data.ingested_last_24h)} detections in the last 24 h
              </p>
            </div>
            <div className="card">
              <p className="card-title">Events waiting to be mapped</p>
              <p className="card-value">{formatCount(freshness.data.mapping_queue)}</p>
              <p className="card-hint">Last mapped {formatAge(freshness.data.last_mapping, now)}</p>
            </div>
            <div className="card">
              <p className="card-title">Latest quality run</p>
              <p className="card-value">{formatAge(freshness.data.last_quality_run, now)}</p>
              <p className="card-hint">{formatDateTime(freshness.data.last_quality_run)}</p>
            </div>
          </div>
        )}
      </section>

      <section aria-labelledby="checks-heading">
        <h2 id="checks-heading">Data-quality checks</h2>
        <p className="muted">
          Result of the most recent run of each check suite. A blocking failure stops new
          data from being published to the lakehouse tables; the previous version stays in
          place.
        </p>
        {checks.error && <p className="error">{checks.error}</p>}
        {checks.data && groups.length === 0 && (
          <p className="muted">No quality run recorded yet.</p>
        )}
        {groups.map(([suite, items]) => (
          <div key={suite} className="table-scroll">
            <table>
              <caption>{suite}</caption>
              <thead>
                <tr>
                  <th scope="col">Check</th>
                  <th scope="col">Result</th>
                  <th scope="col">Observed</th>
                  <th scope="col">When</th>
                </tr>
              </thead>
              <tbody>
                {items.map((check) => {
                  const { text, tone } = verdict(check);
                  return (
                    <tr key={check.check_name}>
                      <td>{check.check_name}</td>
                      <td>
                        <span className={`badge badge--${tone}`}>{text}</span>
                      </td>
                      <td className="wrap">{observed(check)}</td>
                      <td>{formatDateTime(check.checked_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ))}
      </section>
    </div>
  );
}
