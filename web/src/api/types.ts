/** Shapes of the NarSat REST API responses (see /api/v1 in the OpenAPI document). */
import type { Feature, FeatureCollection, MultiPolygon, Point, Polygon } from "geojson";

export type Method = "dnbr" | "model";
export type EventStatus = "active" | "cooling" | "closed" | "merged" | "dissolved";
export type MappingStatus =
  | "not_ready"
  | "pending"
  | "imagery_pending"
  | "mapped"
  | "failed"
  | "skipped";
export type QaStatus = "auto" | "accepted" | "rejected";
export type RasterKind = "pre" | "post" | "dnbr" | "mask" | "probability";

export interface EventProperties {
  event_id: string;
  status: EventStatus;
  mapping_status: MappingStatus;
  mapping_note: string | null;
  first_seen: string;
  last_seen: string;
  n_hotspots: number;
  n_high_confidence: number;
  frp_max_mw: number | null;
  frp_sum_mw: number | null;
  sensors: string[] | null;
  hull_area_ha: number | null;
  country_iso3: string | null;
  admin1_name: string | null;
  burned_area_ha: Partial<Record<Method, number>> | null;
}

export interface ValidationMetric {
  ref_source: string;
  iou: number | null;
  precision: number | null;
  recall: number | null;
  ref_ha: number | null;
  n_ref_perimeters: number;
}

export interface BurnedAreaProduct {
  ba_id: string;
  method: Method;
  model_version: string;
  pre_item_ids: string[] | null;
  post_item_ids: string[] | null;
  pre_datetime: string | null;
  post_datetime: string;
  threshold: number | null;
  mean_score: number | null;
  clear_fraction: number;
  area_ha: number;
  n_polygons: number;
  qa_status: QaStatus;
  qa_at: string | null;
  qa_note: string | null;
  params: Record<string, unknown> | null;
  updated_at: string;
  validation: ValidationMetric[];
}

export interface RasterAsset {
  kind: RasterKind;
  method: Method | null;
  uri: string;
  epsg: number;
  width: number;
  height: number;
  bands: string[];
  created_at: string;
  /** Ground extent of the raster: west, south, east, north (degrees). */
  bounds: [number, number, number, number];
  /** Rendering name -> XYZ tile URL template served by TiTiler. */
  tiles: Record<string, string>;
}

export interface EventDetail extends EventProperties {
  merged_into: string | null;
  mapped_at: string | null;
  mapping_attempts: number;
  seed_hotspot_id: string;
  hull: Polygon | MultiPolygon | null;
  centroid: Point | null;
  bbox: [number, number, number, number] | null;
  burned_areas: BurnedAreaProduct[];
  rasters: RasterAsset[];
}

export interface HotspotProperties {
  acq_ts: string;
  instrument: string;
  satellite: string;
  frp_mw: number | null;
  confidence_level: "low" | "nominal" | "high";
  daynight: string | null;
}

export type HotspotCollection = FeatureCollection<Point, HotspotProperties>;
export type EventFeature = Feature<Polygon | MultiPolygon | Point, EventProperties>;
export type EventCollection = FeatureCollection<Polygon | MultiPolygon | Point, EventProperties>;
export type BurnedAreaCollection = FeatureCollection<
  Polygon | MultiPolygon,
  { method: Method; model_version: string; area_ha: number; qa_status: QaStatus; post_datetime: string }
>;

export interface Summary {
  hotspots: number;
  static_hotspots: number;
  events: number;
  active_events: number;
  mapped_events: number;
  burned_area_ha: Partial<Record<Method, number>>;
  first_acquisition: string | null;
  last_acquisition: string | null;
}

export interface ActivityDay {
  activity_date: string;
  country_iso3: string;
  n_hotspots: number;
  n_events: number;
  frp_sum_mw: number | null;
}

export interface BurnedAreaRow {
  fire_year: number;
  country_iso3: string;
  admin1_name: string;
  method: Method;
  n_events: number;
  area_ha: number;
}

export interface EventSummaryRow {
  event_id: string;
  country_iso3: string;
  admin1_name: string;
  first_seen: string;
  last_seen: string;
  duration_hours: number;
  n_hotspots: number;
  frp_max_mw: number | null;
  area_dnbr_ha: number | null;
  area_model_ha: number | null;
  method_iou: number | null;
}

export interface QualityCheck {
  suite: string;
  check_name: string;
  /** "warn" is a failed check of severity "warn": recorded, but it does not block. */
  status: "pass" | "warn" | "fail";
  /** "error" checks block publication when they fail. */
  severity: "warn" | "error";
  observed: Record<string, unknown> | null;
  detail: string | null;
  checked_at: string;
}

export interface Freshness {
  last_acquisition: string | null;
  last_ingestion: string | null;
  ingested_last_24h: number;
  mapping_queue: number;
  last_mapping: string | null;
  last_quality_run: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  role: "analyst" | "admin";
}

export interface Session {
  token: string;
  email: string;
  role: "analyst" | "admin";
  /** Epoch milliseconds after which the token is no longer accepted. */
  expiresAt: number;
}

export interface Period {
  /** ISO 8601 instants (UTC); null means unbounded. */
  start: string | null;
  end: string | null;
}
