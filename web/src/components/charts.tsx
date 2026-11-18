/** A dependency-free SVG bar chart: enough for daily counts, and trivial to audit. */

export interface Bar {
  label: string;
  value: number;
}

interface BarChartProps {
  bars: Bar[];
  /** Accessible description of what the chart shows. */
  title: string;
  unit: string;
  /** Size in CSS pixels at which the chart is drawn; it scales to its container. Use the
   *  real display width so that the labels keep a readable size. */
  width?: number;
  height?: number;
  color?: string;
}

const tickFormat = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 1 });

/**
 * One bar per day from `first` to `last` (YYYY-MM-DD), days without data at zero, so that
 * the horizontal axis is proportional to time. Spans longer than `maxDays` are grouped by
 * calendar month instead (label YYYY-MM).
 */
/** Whether `fillDays` grouped the bars by month (labels "YYYY-MM") rather than by day. */
export function isMonthly(bars: Bar[]): boolean {
  return bars[0]?.label.length === 7;
}

export function fillDays(values: Map<string, number>, maxDays = 400): Bar[] {
  const days = [...values.keys()].sort();
  const first = days[0];
  const last = days[days.length - 1];
  if (!first || !last) return [];
  const start = Date.parse(`${first}T00:00:00Z`);
  const end = Date.parse(`${last}T00:00:00Z`);
  const byMonth = (end - start) / 86_400_000 + 1 > maxDays;
  const bars = new Map<string, number>();
  for (let t = start; t <= end; t += 86_400_000) {
    const day = new Date(t).toISOString().slice(0, 10);
    const key = byMonth ? day.slice(0, 7) : day;
    bars.set(key, (bars.get(key) ?? 0) + (values.get(day) ?? 0));
  }
  return [...bars.entries()].map(([label, value]) => ({ label, value }));
}

/** A "nice" upper bound for an axis: 1, 2 or 5 times a power of ten. */
export function niceCeiling(value: number): number {
  if (!(value > 0)) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 5, 10]) {
    if (value <= step * magnitude) return step * magnitude;
  }
  return 10 * magnitude;
}

export function BarChart({
  bars,
  title,
  unit,
  width = 720,
  height = 180,
  color = "#f77f00",
}: BarChartProps) {
  if (bars.length === 0) return <p className="muted">No data for this period.</p>;

  const margin = { top: 10, right: 8, bottom: 22, left: 46 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const top = niceCeiling(Math.max(...bars.map((bar) => bar.value)));
  const slot = plotWidth / bars.length;
  const barWidth = Math.max(1, Math.min(28, slot * 0.8));
  const first = bars[0];
  const last = bars[bars.length - 1];

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="chart" role="img" aria-label={title}>
      <title>{title}</title>
      {[0, 0.5, 1].map((fraction) => {
        const y = margin.top + plotHeight * (1 - fraction);
        return (
          <g key={fraction}>
            <line x1={margin.left} x2={width - margin.right} y1={y} y2={y} className="chart-grid" />
            <text x={margin.left - 6} y={y + 4} textAnchor="end" className="chart-label">
              {tickFormat.format(top * fraction)}
            </text>
          </g>
        );
      })}
      {bars.map((bar, index) => {
        const barHeight = (bar.value / top) * plotHeight;
        return (
          <rect
            key={bar.label}
            x={margin.left + index * slot + (slot - barWidth) / 2}
            y={margin.top + plotHeight - barHeight}
            width={barWidth}
            height={barHeight}
            fill={color}
          >
            <title>{`${bar.label}: ${bar.value.toLocaleString("en-GB")} ${unit}`}</title>
          </rect>
        );
      })}
      {first && (
        <text x={margin.left} y={height - 6} className="chart-label">
          {first.label}
        </text>
      )}
      {last && last !== first && (
        <text x={width - margin.right} y={height - 6} textAnchor="end" className="chart-label">
          {last.label}
        </text>
      )}
    </svg>
  );
}
