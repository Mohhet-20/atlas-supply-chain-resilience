import { call, type Params } from "./registryApi";
import type * as L from "./leadtimeTypes";

const P = "/leadtime";
const get = <T>(path: string, params?: Params, signal?: AbortSignal) => call<T>("GET", path, { prefix: P, params, signal });

export interface Scope {
  supplier_id?: number;
  destination_facility_id?: number;
  product_id?: number;
  definition?: string;
  unit?: string;
  commitment?: string;
  grace_hours?: number;
  timezone?: string;
}
export interface AssessBody {
  delivery_ref?: string;
  product_id: number;
  supplier_id?: number;
  destination_facility_id?: number;
  prediction_point: L.PredictionPoint;
  leadtime_run_id?: number;
  risk_run_id?: number;
}
export interface ImportOpts {
  kind: "DELIVERIES" | "EVENTS";
  file: File;
  sourceLabel?: string;
  timeZone: string;
}
const ip = (o: ImportOpts, mode: "preview" | "commit"): Params => ({
  kind: o.kind,
  mode,
  format: o.file.name.toLowerCase().endsWith(".json") ? "JSON" : "CSV",
  filename: o.file.name,
  source_label: o.sourceLabel || o.file.name,
  time_zone: o.timeZone,
});
const ct = (f: File) => (f.name.toLowerCase().endsWith(".json") ? "application/json" : "text/csv");

export const leadtimeApi = {
  limits: (s?: AbortSignal) => get<L.LeadtimeLimits>("/limits", undefined, s),
  summary: (p: Scope, s?: AbortSignal) => get<L.Summary>("/analytics/summary", { ...p }, s),
  groups: (by: string, p: Scope, s?: AbortSignal) => get<L.Groups>("/analytics/groups", { by, ...p }, s),
  trend: (p: Scope, s?: AbortSignal) => get<L.Trend>("/analytics/trend", { ...p }, s),
  deliveries: (p: { limit: number; offset: number; state?: string; q?: string; supplier_id?: number }, s?: AbortSignal) => get<L.Page<L.DeliveryRow>>("/deliveries", { ...p }, s),
  reliability: (supplierId: number, s?: AbortSignal) => get<L.Reliability>(`/suppliers/${supplierId}/reliability`, undefined, s),
  runs: (p: { limit: number; offset: number; target?: string }, s?: AbortSignal) => get<L.Page<L.RunSummary>>("/runs", { ...p }, s),
  run: (id: number, s?: AbortSignal) => get<L.RunDetail>(`/runs/${id}`, undefined, s),
  start: (config: Record<string, unknown>) => call<L.RunSummary>("POST", "/runs", { prefix: P, body: config }),
  assessments: (p: { limit: number; offset: number }, s?: AbortSignal) => get<L.Page<L.Assessment>>("/assessments", { ...p }, s),
  assess: (b: AssessBody) => call<L.Assessment>("POST", "/assessments", { prefix: P, body: b }),
  preview: (o: ImportOpts) => call<L.ImportPreview>("POST", "/imports", { prefix: P, params: ip(o, "preview"), raw: { data: o.file, contentType: ct(o.file) } }),
  commit: (o: ImportOpts) => call<L.ImportCommit>("POST", "/imports", { prefix: P, params: ip(o, "commit"), raw: { data: o.file, contentType: ct(o.file) } }),
  batches: (limit: number, offset: number, s?: AbortSignal) => get<L.Page<L.ImportBatch>>("/imports", { limit, offset }, s),
};
