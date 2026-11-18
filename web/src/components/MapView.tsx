import "maplibre-gl/dist/maplibre-gl.css";

import type { FeatureCollection } from "geojson";
import {
  MapLibreMap,
  NavigationControl,
  ScaleControl,
  setWorkerUrl,
  type GeoJSONSource,
  type StyleSpecification,
  type VectorTileSource,
} from "maplibre-gl";
// MapLibre GL 6 ships its web worker as a separate file and looks for it next to its
// own module. Once the library is bundled that location no longer exists, so the worker
// is emitted as an asset of this application and its URL is passed explicitly.
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import { useEffect, useRef, useState } from "react";

import { absoluteUrl, vectorTileUrl, type VectorLayer } from "../api/client";
import type { EventDetail, Method, Period } from "../api/types";
import {
  AOI_BOUNDS,
  BASEMAP_STYLE_URL,
  CLICK_TARGETS,
  COLORS,
  DATA_ATTRIBUTION,
  DATA_LAYERS,
  FALLBACK_STYLE,
  LAYER_GROUPS,
  paddedBounds,
} from "../lib/layers";

setWorkerUrl(maplibreWorkerUrl);

const VECTOR_LAYERS: VectorLayer[] = ["burned_areas", "events", "hotspots"];
const RASTER_ID = "event-raster";
const SELECTED_ID = "selected-event";

export interface MapViewProps {
  period: Period;
  /** Burned-area method shown on the map; null shows both. */
  method: Method | null;
  visible: Record<VectorLayer, boolean>;
  selected: EventDetail | null;
  /** Raster shown for the selected event, if any: XYZ tile URL template and extent. */
  overlay: { url: string; bounds: [number, number, number, number] } | null;
  overlayOpacity: number;
  onSelectEvent: (eventId: string | null) => void;
}

/** The base-map style if it can be fetched, otherwise a plain background. */
async function resolveBasemap(): Promise<{ style: string | StyleSpecification; fallback: boolean }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 6000);
  try {
    const response = await fetch(BASEMAP_STYLE_URL, { signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    await response.json();
    return { style: BASEMAP_STYLE_URL, fallback: false };
  } catch {
    return { style: FALLBACK_STYLE, fallback: true };
  } finally {
    clearTimeout(timer);
  }
}

export function MapView({
  period,
  method,
  visible,
  selected,
  overlay,
  overlayOpacity,
  onSelectEvent,
}: MapViewProps) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const selectRef = useRef(onSelectEvent);
  const fittedEvent = useRef<string | null>(null);
  const [ready, setReady] = useState(false);
  const [plainBasemap, setPlainBasemap] = useState(false);

  useEffect(() => {
    selectRef.current = onSelectEvent;
  }, [onSelectEvent]);

  // --- create the map once -----------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    let map: MapLibreMap | null = null;

    void resolveBasemap().then(({ style, fallback }) => {
      if (cancelled || !container.current) return;
      setPlainBasemap(fallback);
      map = new MapLibreMap({
        container: container.current,
        style,
        bounds: AOI_BOUNDS,
        fitBoundsOptions: { padding: 24 },
        maxZoom: 17,
        dragRotate: false,
        pitchWithRotate: false,
        attributionControl: { compact: true, customAttribution: DATA_ATTRIBUTION },
      });
      const created = map;
      created.addControl(new NavigationControl({ showCompass: false }), "top-right");
      created.addControl(new ScaleControl({ unit: "metric" }), "bottom-left");
      created.on("load", () => {
        if (cancelled) return;
        mapRef.current = created;
        setReady(true);
      });
      created.on("click", (event) => {
        const layers = CLICK_TARGETS.map((target) => target.layer).filter((id) =>
          created.getLayer(id),
        );
        const features = created.queryRenderedFeatures(event.point, { layers });
        // The first target that has a feature under the pointer wins: a detection,
        // then a burned area, then the outline of an event.
        for (const target of CLICK_TARGETS) {
          const hit = features.find((feature) => feature.layer.id === target.layer);
          const id: unknown = hit?.properties[target.property];
          if (typeof id === "string" && id) {
            selectRef.current(id);
            return;
          }
        }
      });
      for (const target of CLICK_TARGETS) {
        created.on("mouseenter", target.layer, () => {
          created.getCanvas().style.cursor = "pointer";
        });
        created.on("mouseleave", target.layer, () => {
          created.getCanvas().style.cursor = "";
        });
      }
    });

    return () => {
      cancelled = true;
      mapRef.current = null;
      map?.remove();
    };
  }, []);

  // --- vector layers: created once, tile URLs follow the period and method -----------
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    for (const layer of VECTOR_LAYERS) {
      const url = vectorTileUrl(layer, period, layer === "burned_areas" ? method : null);
      const source = map.getSource(layer) as VectorTileSource | undefined;
      if (source) source.setTiles([url]);
      else map.addSource(layer, { type: "vector", tiles: [url], minzoom: 0, maxzoom: 14 });
    }
    for (const spec of DATA_LAYERS) {
      if (!map.getLayer(spec.id)) map.addLayer(spec);
    }
  }, [ready, period, method]);

  // --- layer toggles ----------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    for (const [group, layerIds] of Object.entries(LAYER_GROUPS)) {
      const shown = visible[group as VectorLayer];
      for (const id of layerIds) {
        if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", shown ? "visible" : "none");
      }
    }
  }, [ready, visible]);

  // --- outline of the selected event, and move the map to it -----------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    const data: FeatureCollection = {
      type: "FeatureCollection",
      features: selected?.hull ? [{ type: "Feature", properties: {}, geometry: selected.hull }] : [],
    };
    const source = map.getSource(SELECTED_ID) as GeoJSONSource | undefined;
    if (source) {
      void source.setData(data);
    } else {
      map.addSource(SELECTED_ID, { type: "geojson", data });
      map.addLayer({
        id: `${SELECTED_ID}-line`,
        type: "line",
        source: SELECTED_ID,
        paint: { "line-color": COLORS.selected, "line-width": 3 },
      });
    }
    const bounds = selected ? paddedBounds(selected, 0.02) : null;
    if (selected && bounds && fittedEvent.current !== selected.event_id) {
      // The map is hidden while another tab is shown ("Show on map" on the Statistics
      // page). Its canvas keeps the size it had then until the next resize, which would
      // make fitBounds compute the camera for the wrong size: resize first.
      map.resize();
      map.fitBounds(bounds, { padding: 48, maxZoom: 13, duration: 600 });
    }
    fittedEvent.current = selected?.event_id ?? null;
  }, [ready, selected]);

  // --- raster overlay of the selected event (served by TiTiler) ----------------------------
  const overlayUrl = overlay?.url ?? null;
  const overlayBounds = overlay ? overlay.bounds.join(",") : "";
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    if (map.getLayer(RASTER_ID)) map.removeLayer(RASTER_ID);
    if (map.getSource(RASTER_ID)) map.removeSource(RASTER_ID);
    if (!overlayUrl || !overlayBounds) return;
    map.addSource(RASTER_ID, {
      type: "raster",
      tiles: [absoluteUrl(overlayUrl)],
      tileSize: 256,
      minzoom: 6,
      maxzoom: 16,
      // TiTiler answers 404 for a tile outside the image: only request those that exist.
      bounds: overlayBounds.split(",").map(Number) as [number, number, number, number],
      attribution: "Contains modified Copernicus Sentinel data",
    });
    // Under the vector layers, so detections and outlines stay readable.
    const firstDataLayer = DATA_LAYERS.find((spec) => map.getLayer(spec.id))?.id;
    map.addLayer({ id: RASTER_ID, type: "raster", source: RASTER_ID }, firstDataLayer);
  }, [ready, overlayUrl, overlayBounds]);

  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map || !map.getLayer(RASTER_ID)) return;
    map.setPaintProperty(RASTER_ID, "raster-opacity", overlayOpacity);
  }, [ready, overlayOpacity, overlayUrl, overlayBounds]);

  return (
    <div className="map-wrap">
      <div ref={container} className="map" role="application" aria-label="Map of fire detections" />
      {!ready && <div className="map-status">Loading map…</div>}
      {ready && plainBasemap && (
        <div className="map-status map-status--note">
          Base map unavailable (offline?). Fire data is shown on a plain background.
        </div>
      )}
    </div>
  );
}
