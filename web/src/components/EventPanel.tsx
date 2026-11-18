import { useMemo, useState } from "react";

import { api } from "../api/client";
import type { BurnedAreaProduct, EventDetail, QaStatus, Session } from "../api/types";
import {
  formatCount,
  formatDate,
  formatDateTime,
  formatHa,
  formatMw,
  formatPercent,
  formatScore,
  label,
} from "../lib/format";
import { overlayChoices } from "../lib/layers";
import { useAsync } from "../lib/useAsync";
import { BarChart, fillDays, type Bar } from "./charts";

interface EventPanelProps {
  event: EventDetail | null;
  loading: boolean;
  error: string | null;
  session: Session | null;
  overlayKey: string | null;
  overlayOpacity: number;
  onOverlayChange: (key: string | null) => void;
  onOpacityChange: (opacity: number) => void;
  onChanged: () => void;
  onClose: () => void;
}

/** Detections per UTC day, with empty days in between filled in. */
export function detectionsPerDay(timestamps: string[]): Bar[] {
  const counts = new Map<string, number>();
  for (const stamp of timestamps) {
    const instant = new Date(stamp);
    if (Number.isNaN(instant.getTime())) continue;
    // The UTC day of the instant, whatever offset the timestamp is written with.
    const day = instant.toISOString().slice(0, 10);
    counts.set(day, (counts.get(day) ?? 0) + 1);
  }
  return fillDays(counts);
}

function referenceName(source: string): string {
  if (source === "narsat:dnbr") return "dNBR product of this event";
  return `Reference perimeters (${source})`;
}

function ProductCard({
  product,
  session,
  onChanged,
}: {
  product: BurnedAreaProduct;
  session: Session | null;
  onChanged: () => void;
}) {
  const [note, setNote] = useState(product.qa_note ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function review(status: QaStatus) {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      await api.review(session.token, product.ba_id, status, note.trim() || null);
      onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className={`product product--${product.method}`}>
      <header>
        <h4>{label(product.method)}</h4>
        <span className={`badge badge--${product.qa_status}`}>{label(product.qa_status)}</span>
      </header>
      <p className="product-area">{formatHa(product.area_ha)}</p>
      <dl className="facts">
        <dt>Post-fire image</dt>
        <dd>{formatDate(product.post_datetime)}</dd>
        <dt>Pre-fire image</dt>
        <dd>{formatDate(product.pre_datetime)}</dd>
        <dt>Clear sky over the area</dt>
        <dd>{formatPercent(product.clear_fraction)}</dd>
        <dt>Threshold</dt>
        <dd>{product.threshold === null ? "–" : product.threshold.toFixed(2)}</dd>
        <dt>Polygons</dt>
        <dd>{formatCount(product.n_polygons)}</dd>
        <dt>Version</dt>
        <dd>{product.model_version}</dd>
      </dl>
      {product.validation.length > 0 && (
        <table className="compact">
          <caption>Agreement</caption>
          <thead>
            <tr>
              <th scope="col">Compared with</th>
              <th scope="col">IoU</th>
              <th scope="col">Precision</th>
              <th scope="col">Recall</th>
            </tr>
          </thead>
          <tbody>
            {product.validation.map((metric) => (
              <tr key={metric.ref_source}>
                <td>{referenceName(metric.ref_source)}</td>
                <td>{formatScore(metric.iou)}</td>
                <td>{formatScore(metric.precision)}</td>
                <td>{formatScore(metric.recall)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {product.qa_note && !session && <p className="muted">Review note: {product.qa_note}</p>}
      {session && (
        <div className="review">
          <label>
            Review note
            <input
              value={note}
              maxLength={1000}
              onChange={(event) => setNote(event.target.value)}
              placeholder="Optional"
            />
          </label>
          <div className="button-row">
            <button type="button" disabled={busy} onClick={() => void review("accepted")}>
              Accept
            </button>
            <button type="button" disabled={busy} className="danger" onClick={() => void review("rejected")}>
              Reject
            </button>
            <button type="button" disabled={busy} className="quiet" onClick={() => void review("auto")}>
              Clear review
            </button>
          </div>
          {error && <p className="error">{error}</p>}
        </div>
      )}
    </article>
  );
}

export function EventPanel({
  event,
  loading,
  error,
  session,
  overlayKey,
  overlayOpacity,
  onOverlayChange,
  onOpacityChange,
  onChanged,
  onClose,
}: EventPanelProps) {
  const eventId = event?.event_id ?? null;
  const hotspots = useAsync(eventId, (signal) => api.eventHotspots(eventId ?? "", signal));
  const choices = useMemo(() => overlayChoices(event?.rasters ?? []), [event]);
  const timeline = useMemo(
    () => detectionsPerDay((hotspots.data?.features ?? []).map((f) => f.properties.acq_ts)),
    [hotspots.data],
  );
  const [requeueState, setRequeueState] = useState<string | null>(null);

  async function requeue() {
    if (!session || !event) return;
    try {
      await api.requeue(session.token, event.event_id);
      setRequeueState("Queued again: the next mapping run will pick it up.");
      onChanged();
    } catch (reason) {
      setRequeueState(reason instanceof Error ? reason.message : String(reason));
    }
  }

  return (
    <aside className="panel" aria-label="Selected fire event">
      <header className="panel-header">
        <h2>Fire event</h2>
        <button type="button" className="quiet" onClick={onClose} aria-label="Close event panel">
          Close
        </button>
      </header>

      {loading && !event && <p className="muted">Loading…</p>}
      {error && <p className="error">{error}</p>}

      {event && (
        <>
          <p className="event-place">
            {event.admin1_name ?? "Unknown area"}
            {event.country_iso3 ? `, ${event.country_iso3}` : ""}
          </p>
          <p className="badges">
            <span className={`badge badge--${event.status}`}>{label(event.status)}</span>
            <span className={`badge badge--${event.mapping_status}`}>
              {label(event.mapping_status)}
            </span>
          </p>
          <dl className="facts">
            <dt>First detection</dt>
            <dd>{formatDateTime(event.first_seen)}</dd>
            <dt>Last detection</dt>
            <dd>{formatDateTime(event.last_seen)}</dd>
            <dt>Detections</dt>
            <dd>
              {formatCount(event.n_hotspots)} ({formatCount(event.n_high_confidence)} high
              confidence)
            </dd>
            <dt>Peak radiative power</dt>
            <dd>{formatMw(event.frp_max_mw)}</dd>
            <dt>Area enclosing detections</dt>
            <dd>{formatHa(event.hull_area_ha)}</dd>
            <dt>Sensors</dt>
            <dd>{event.sensors?.join(", ") || "–"}</dd>
          </dl>
          {event.mapping_note && <p className="muted">Mapping note: {event.mapping_note}</p>}

          <section>
            <h3>Detections per day (UTC)</h3>
            {hotspots.error ? (
              <p className="error">{hotspots.error}</p>
            ) : (
              <BarChart
                bars={timeline}
                title="Number of satellite fire detections per day for this event"
                unit="detections"
                width={360}
                height={140}
              />
            )}
          </section>

          <section>
            <h3>Burned area</h3>
            {event.burned_areas.length === 0 ? (
              <p className="muted">
                Not mapped yet. A fire is mapped once it has gone quiet and a cloud-free
                Sentinel-2 image taken after it is available.
              </p>
            ) : (
              event.burned_areas.map((product) => (
                <ProductCard
                  key={`${product.ba_id}:${product.qa_status}:${product.qa_note ?? ""}`}
                  product={product}
                  session={session}
                  onChanged={onChanged}
                />
              ))
            )}
          </section>

          {choices.length > 0 && (
            <section>
              <h3>Satellite imagery</h3>
              <label>
                Layer on the map
                <select
                  value={overlayKey ?? ""}
                  onChange={(change) => onOverlayChange(change.target.value || null)}
                >
                  <option value="">None</option>
                  {choices.map((choice) => (
                    <option key={choice.key} value={choice.key}>
                      {choice.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Opacity
                <input
                  type="range"
                  min={0.1}
                  max={1}
                  step={0.05}
                  value={overlayOpacity}
                  disabled={!overlayKey}
                  onChange={(change) => onOpacityChange(Number(change.target.value))}
                />
              </label>
            </section>
          )}

          {session?.role === "admin" && (
            <section>
              <button type="button" className="quiet" onClick={() => void requeue()}>
                Map this event again
              </button>
              {requeueState && <p className="muted">{requeueState}</p>}
            </section>
          )}
          <p className="muted small">Event {event.event_id}</p>
        </>
      )}
    </aside>
  );
}
