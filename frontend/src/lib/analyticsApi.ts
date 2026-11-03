import { API_BASE_URL } from "./config";
import { call, tokenStore, type Params as QueryParams } from "./registryApi";
import type * as T from "./analyticsTypes";

const P = "/analytics";
const get = <X>(path: string, params?: QueryParams, signal?: AbortSignal) => call<X>("GET", path, { prefix: P, params, signal });
type Q = QueryParams;

export const analyticsApi = {
  health: (s?: AbortSignal) => get<T.Health>("/health", undefined, s),
  metrics: (s?: AbortSignal) => get<T.MetricsRegistry>("/metrics", undefined, s),
  filters: (s?: AbortSignal) => get<T.FilterValues>("/filters", undefined, s),
  overview: (q: Q, s?: AbortSignal) => get<T.Overview>("/overview", q, s),
  suppliers: (q: Q, s?: AbortSignal) => get<T.SuppliersView>("/suppliers", q, s),
  inventory: (q: Q, s?: AbortSignal) => get<T.InventoryView>("/inventory", q, s),
  logistics: (q: Q, s?: AbortSignal) => get<T.LogisticsView>("/logistics", q, s),
  network: (q: Q, s?: AbortSignal) => get<T.NetworkView>("/network", q, s),
  disruption: (q: Q, s?: AbortSignal) => get<T.DisruptionView>("/disruption", q, s),
  trends: (q: Q, s?: AbortSignal) => get<{ meta: T.Meta; supplier_risk: T.RiskTrend; activity: { dates: string[]; series: Record<string, number[]>; basis: string } }>("/trends", q, s),
  alerts: (q: Q, s?: AbortSignal) => get<T.AlertsView>("/alerts", q, s),
  activity: (q: Q, s?: AbortSignal) => get<T.ActivityView>("/activity", q, s),
};

/** Export is fetched with the bearer token (never in the URL) and saved from a Blob. */
export async function downloadExport(area: string, format: "csv" | "json", q: Q): Promise<string> {
  const u = new URL(`${API_BASE_URL}${P}/export`);
  for (const [k, v] of Object.entries({ ...q, area, format })) if (v !== undefined && v !== null && v !== "") u.searchParams.set(k, String(v));
  const headers: Record<string, string> = {};
  const token = tokenStore.get();
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(u.toString(), { headers });
  if (!r.ok) {
    let msg = `Export failed with status ${r.status}.`;
    try { msg = ((await r.json()) as { error?: { message?: string } }).error?.message ?? msg; } catch { /* keep default */ }
    throw new Error(msg);
  }
  const blob = await r.blob();
  const name = `atlas-${area}.${format}`;
  if (typeof URL.createObjectURL === "function") {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
  }
  return name;
}
