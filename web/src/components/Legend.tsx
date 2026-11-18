import type { VectorLayer } from "../api/client";
import type { Method } from "../api/types";
import { COLORS } from "../lib/layers";

interface LegendProps {
  visible: Record<VectorLayer, boolean>;
  method: Method | null;
  onToggle: (layer: VectorLayer) => void;
  onMethodChange: (method: Method | null) => void;
}

function Swatch({ color, shape = "square" }: { color: string; shape?: "square" | "dot" | "line" }) {
  return <span className={`swatch swatch--${shape}`} style={{ background: color }} aria-hidden="true" />;
}

/** Layer switches and the meaning of the colours, as a card over the map. */
export function Legend({ visible, method, onToggle, onMethodChange }: LegendProps) {
  return (
    <section className="legend" aria-label="Map layers">
      <label className="legend-row">
        <input type="checkbox" checked={visible.hotspots} onChange={() => onToggle("hotspots")} />
        Fire detections, by confidence
      </label>
      <p className="legend-keys">
        <Swatch color={COLORS.high} shape="dot" /> high
        <Swatch color={COLORS.nominal} shape="dot" /> nominal
        <Swatch color={COLORS.low} shape="dot" /> low
      </p>

      <label className="legend-row">
        <input type="checkbox" checked={visible.events} onChange={() => onToggle("events")} />
        Fire events (outline)
      </label>
      <p className="legend-keys">
        <Swatch color={COLORS.active} shape="line" /> active
        <Swatch color={COLORS.cooling} shape="line" /> cooling
        <Swatch color={COLORS.closed} shape="line" /> closed
      </p>

      <label className="legend-row">
        <input
          type="checkbox"
          checked={visible.burned_areas}
          onChange={() => onToggle("burned_areas")}
        />
        Burned areas
      </label>
      <p className="legend-keys">
        <Swatch color={COLORS.dnbr} /> dNBR
        <Swatch color={COLORS.model} /> model
      </p>
      <label className="legend-select">
        <span className="visually-hidden">Burned-area method</span>
        <select
          value={method ?? ""}
          onChange={(change) => onMethodChange((change.target.value || null) as Method | null)}
          disabled={!visible.burned_areas}
        >
          <option value="">Both methods</option>
          <option value="dnbr">dNBR only</option>
          <option value="model">Model only</option>
        </select>
      </label>
    </section>
  );
}
