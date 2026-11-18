/** Thin, typed client of the NarSat REST API. No state: the session token is passed in. */
import type {
  ActivityDay,
  BurnedAreaCollection,
  BurnedAreaRow,
  EventCollection,
  EventDetail,
  EventSummaryRow,
  Freshness,
  HotspotCollection,
  Method,
  Period,
  QaStatus,
  QualityCheck,
  Summary,
  TokenResponse,
} from "./types";

export type QueryValue = string | number | boolean | null | undefined;
export type VectorLayer = "hotspots" | "events" | "burned_areas";

export const API_BASE: string = (import.meta.env?.VITE_API_BASE_URL ?? "/api/v1").replace(
  /\/+$/,
  "",
);

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** `?a=1&b=x` from an object, skipping empty values. Returns "" when nothing is left. */
export function buildQuery(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

/**
 * MapLibre fetches tiles from a worker, where relative URLs have no base. A template
 * such as `/api/v1/tiles/{z}/{x}/{y}.mvt` is therefore prefixed with the page origin.
 * (`new URL()` cannot be used: it would percent-encode the `{z}` placeholders.)
 */
export function absoluteUrl(template: string, origin: string = globalThis.location?.origin ?? ""): string {
  if (/^https?:\/\//i.test(template)) return template;
  return `${origin}${template.startsWith("/") ? "" : "/"}${template}`;
}

export function vectorTileUrl(layer: VectorLayer, period: Period, method?: Method | null): string {
  const query = buildQuery({ start: period.start, end: period.end, method: method ?? null });
  return absoluteUrl(`${API_BASE}/tiles/${layer}/{z}/{x}/{y}.mvt${query}`);
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      return typeof detail === "string" ? detail : JSON.stringify(detail);
    }
  } catch {
    // Not JSON: fall through to the status text.
  }
  return response.statusText || `HTTP ${response.status}`;
}

interface RequestOptions {
  params?: Record<string, QueryValue>;
  token?: string | null;
  method?: "GET" | "POST";
  json?: unknown;
  form?: Record<string, string>;
  signal?: AbortSignal;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json, application/geo+json" };
  let body: BodyInit | undefined;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.json);
  } else if (options.form) {
    body = new URLSearchParams(options.form);
  }
  if (options.token) headers.Authorization = `Bearer ${options.token}`;

  const response = await fetch(`${API_BASE}${path}${buildQuery(options.params ?? {})}`, {
    method: options.method ?? "GET",
    headers,
    body,
    signal: options.signal,
  });
  if (!response.ok) throw new ApiError(response.status, await errorMessage(response));
  return (await response.json()) as T;
}

export const api = {
  summary: (signal?: AbortSignal) => request<Summary>("/stats/summary", { signal }),
  freshness: (signal?: AbortSignal) => request<Freshness>("/quality/freshness", { signal }),
  checks: (signal?: AbortSignal) => request<QualityCheck[]>("/quality/checks", { signal }),
  activity: (params: { start?: string | null; end?: string | null; country?: string | null }, signal?: AbortSignal) =>
    request<ActivityDay[]>("/stats/activity", { params, signal }),
  burnedArea: (params: { year?: number | null; country?: string | null; method?: Method | null }, signal?: AbortSignal) =>
    request<BurnedAreaRow[]>("/stats/burned-area", { params, signal }),
  eventSummaries: (limit: number, signal?: AbortSignal) =>
    request<EventSummaryRow[]>("/stats/events", { params: { limit }, signal }),
  events: (
    params: { start?: string | null; end?: string | null; country?: string | null; min_hotspots?: number; limit?: number; geometry?: "hull" | "centroid" },
    signal?: AbortSignal,
  ) => request<EventCollection>("/events", { params, signal }),
  event: (id: string, signal?: AbortSignal) => request<EventDetail>(`/events/${id}`, { signal }),
  eventHotspots: (id: string, signal?: AbortSignal) =>
    request<HotspotCollection>(`/events/${id}/hotspots`, { signal }),
  eventBurnedAreas: (id: string, signal?: AbortSignal) =>
    request<BurnedAreaCollection>(`/events/${id}/burned-areas`, { signal }),
  login: (email: string, password: string) =>
    request<TokenResponse>("/auth/token", { method: "POST", form: { username: email, password } }),
  review: (token: string, baId: string, status: QaStatus, note: string | null) =>
    request<{ ba_id: string; qa_status: QaStatus }>(`/burned-areas/${baId}/qa`, {
      method: "POST",
      token,
      json: { status, note },
    }),
  requeue: (token: string, eventId: string) =>
    request<{ event_id: string; mapping_status: string }>(`/events/${eventId}/remap`, {
      method: "POST",
      token,
    }),
};
