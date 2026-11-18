/** Map styling: base-map fallback, vector layers served by the API, raster overlay choices. */
import type { LayerSpecification, StyleSpecification } from "maplibre-gl";

import type { EventDetail, RasterAsset } from "../api/types";

/** Area of interest shown on first load: Morocco, Algeria, Tunisia (west, south, east, north). */
export const AOI_BOUNDS: [number, number, number, number] = [-13.5, 27.0, 11.8, 37.6];

export const BASEMAP_STYLE_URL: string =
  import.meta.env?.VITE_BASEMAP_STYLE_URL ?? "https://tiles.openfreemap.org/styles/liberty";

/** Used when the base-map style cannot be loaded (offline, blocked network). */
export const FALLBACK_STYLE: StyleSpecification = {
  version: 8,
  name: "NarSat plain background",
  sources: {},
  layers: [{ id: "background", type: "background", paint: { "background-color": "#e7ecf2" } }],
};

export const DATA_ATTRIBUTION =
  'Active fires: <a href="https://firms.modaps.eosdis.nasa.gov/" target="_blank" rel="noreferrer">NASA FIRMS</a>' +
  " | Burned areas: NarSat, contains modified Copernicus Sentinel data";

export const COLORS = {
  high: "#d62828",
  nominal: "#f77f00",
  low: "#fcbf49",
  active: "#d62828",
  cooling: "#f77f00",
  closed: "#475569",
  dnbr: "#9a3412",
  model: "#6d28d9",
  selected: "#0ea5e9",
} as const;

/** Layers drawn over the base map, bottom to top. Source ids equal the API layer names. */
export const DATA_LAYERS: LayerSpecification[] = [
  {
    id: "burned-areas-fill",
    type: "fill",
    source: "burned_areas",
    "source-layer": "burned_areas",
    paint: {
      "fill-color": ["match", ["get", "method"], "model", COLORS.model, COLORS.dnbr],
      "fill-opacity": 0.45,
    },
  },
  {
    id: "burned-areas-line",
    type: "line",
    source: "burned_areas",
    "source-layer": "burned_areas",
    paint: {
      "line-color": ["match", ["get", "method"], "model", COLORS.model, COLORS.dnbr],
      "line-width": 1,
    },
  },
  {
    id: "events-fill",
    type: "fill",
    source: "events",
    "source-layer": "events",
    paint: { "fill-color": "#0b1f3a", "fill-opacity": 0.04 },
  },
  {
    id: "events-line",
    type: "line",
    source: "events",
    "source-layer": "events",
    paint: {
      "line-color": [
        "match",
        ["get", "status"],
        "active",
        COLORS.active,
        "cooling",
        COLORS.cooling,
        COLORS.closed,
      ],
      "line-width": ["interpolate", ["linear"], ["zoom"], 4, 1, 12, 2],
      "line-dasharray": [2, 1],
    },
  },
  {
    id: "hotspots-circle",
    type: "circle",
    source: "hotspots",
    "source-layer": "hotspots",
    paint: {
      "circle-color": [
        "match",
        ["get", "confidence_level"],
        "high",
        COLORS.high,
        "low",
        COLORS.low,
        COLORS.nominal,
      ],
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 1.5, 8, 3, 13, 6],
      "circle-opacity": 0.85,
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 6, 0, 10, 0.75],
    },
  },
];

/** Which map layers belong to each toggle of the legend. */
export const LAYER_GROUPS = {
  hotspots: ["hotspots-circle"],
  events: ["events-fill", "events-line"],
  burned_areas: ["burned-areas-fill", "burned-areas-line"],
} as const;

/** Layers that react to a click, with the feature property holding the event identifier. */
export const CLICK_TARGETS: { layer: string; property: string }[] = [
  { layer: "hotspots-circle", property: "event_id" },
  { layer: "burned-areas-fill", property: "event_id" },
  { layer: "events-fill", property: "id" },
];

export interface OverlayChoice {
  /** Stable key: `<kind>:<method or ->:<rendering>`. */
  key: string;
  label: string;
  url: string;
  /** Extent of the raster: tiles outside it do not exist and are not requested. */
  bounds: [number, number, number, number];
}

const KIND_LABELS: Record<RasterAsset["kind"], string> = {
  post: "Post-fire image",
  pre: "Pre-fire image",
  dnbr: "Burn severity index (dNBR)",
  mask: "Burn mask",
  probability: "Burn probability",
};

const RENDERING_LABELS: Record<string, string> = {
  true_color: "true colour",
  swir_false_color: "SWIR false colour",
};

const KIND_ORDER: RasterAsset["kind"][] = ["post", "pre", "dnbr", "probability", "mask"];

/** Raster layers that can be shown for an event, in a sensible reading order. */
export function overlayChoices(rasters: RasterAsset[]): OverlayChoice[] {
  const choices: OverlayChoice[] = [];
  const sorted = [...rasters].sort(
    (a, b) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind),
  );
  for (const raster of sorted) {
    for (const [rendering, url] of Object.entries(raster.tiles)) {
      const detail = RENDERING_LABELS[rendering];
      const method = raster.method ? ` (${raster.method === "model" ? "model" : "dNBR"})` : "";
      choices.push({
        key: `${raster.kind}:${raster.method ?? "-"}:${rendering}`,
        label: `${KIND_LABELS[raster.kind]}${method}${detail ? ` - ${detail}` : ""}`,
        url,
        bounds: raster.bounds,
      });
    }
  }
  return choices;
}

/** Bounding box of an event grown by a margin (degrees): the extent of its rasters. */
export function paddedBounds(
  event: Pick<EventDetail, "bbox">,
  margin = 0.03,
): [number, number, number, number] | null {
  if (!event.bbox) return null;
  const [west, south, east, north] = event.bbox;
  return [west - margin, south - margin, east + margin, north + margin];
}
