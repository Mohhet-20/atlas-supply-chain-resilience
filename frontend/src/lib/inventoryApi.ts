import { call, type Params } from "./registryApi";
import type * as K from "./inventoryTypes";

const P = "/inventory";
const get = <T>(path: string, params?: Params, signal?: AbortSignal) => call<T>("GET", path, { prefix: P, params, signal });

export interface ImportOptions {
  kind: K.ImportKind;
  file: File;
  mapping?: Record<string, string>;
  sourceLabel?: string;
  atomic?: boolean;
  dateFormat?: string;
}

function importParams(o: ImportOptions, mode: "preview" | "commit"): Params {
  const name = o.file.name.toLowerCase();
  return {
    kind: o.kind,
    mode,
    format: name.endsWith(".json") ? "JSON" : "CSV",
    filename: o.file.name,
    mapping: o.mapping && Object.keys(o.mapping).length > 0 ? JSON.stringify(o.mapping) : undefined,
    source_label: o.sourceLabel || o.file.name,
    atomic: o.atomic ? true : undefined,
    date_format: o.dateFormat,
  };
}
const ctype = (f: File) => (f.name.toLowerCase().endsWith(".json") ? "application/json" : "text/csv");

export const inventoryApi = {
  limits: (s?: AbortSignal) => get<K.InventoryLimits>("/limits", undefined, s),
  quality: (s?: AbortSignal) => get<K.Quality>("/quality", undefined, s),
  positions: (p: { limit: number; offset: number; product_id?: number; facility_id?: number; latest: boolean }, s?: AbortSignal) =>
    get<K.Page<K.Position> & { latest_only: boolean }>("/positions", { ...p }, s),
  preview: (o: ImportOptions) => call<K.PreviewResponse>("POST", "/imports", { prefix: P, params: importParams(o, "preview"), raw: { data: o.file, contentType: ctype(o.file) } }),
  commit: (o: ImportOptions) => call<K.CommitResponse>("POST", "/imports", { prefix: P, params: importParams(o, "commit"), raw: { data: o.file, contentType: ctype(o.file) } }),
  batches: (limit: number, offset: number, s?: AbortSignal) => get<K.Page<K.ImportBatch>>("/imports", { limit, offset }, s),
  batch: (id: number, s?: AbortSignal) => get<K.ImportBatch>(`/imports/${id}`, undefined, s),
  batchRows: (id: number, limit: number, offset: number, outcome: string, s?: AbortSignal) =>
    get<K.Page<K.BatchRow>>(`/imports/${id}/rows`, { limit, offset, outcome }, s),
  policies: (limit: number, offset: number, includeSuperseded: boolean, s?: AbortSignal) =>
    get<K.Page<K.Policy>>("/policies", { limit, offset, include_superseded: includeSuperseded }, s),
  missingPolicies: (limit: number, offset: number, s?: AbortSignal) => get<K.Page<K.MissingPolicy>>("/policies/missing", { limit, offset }, s),
  list: (limit: number, offset: number, s?: AbortSignal) => get<K.Page<K.Analysis>>("/analyses", { limit, offset }, s),
  get: (id: number, s?: AbortSignal) => get<K.Analysis>(`/analyses/${id}`, undefined, s),
  start: (body: { impact_analysis_id?: number; config?: Partial<K.AnalysisConfig> }) => call<K.Analysis>("POST", "/analyses", { prefix: P, body }),
  cancel: (id: number) => call<K.Analysis>("POST", `/analyses/${id}/cancel`, { prefix: P, body: {} }),
  summary: (id: number, s?: AbortSignal) => get<K.SummaryResponse>(`/analyses/${id}/summary`, undefined, s),
  findings: (id: number, f: K.FindingFilters, s?: AbortSignal) => get<K.Page<K.Finding>>(`/analyses/${id}/findings`, { ...f }, s),
  finding: (id: number, fid: number, s?: AbortSignal) => get<K.FindingDetail>(`/analyses/${id}/findings/${fid}`, undefined, s),
  compare: (baselineId: number, scenarioId: number, limit: number, offset: number, s?: AbortSignal) =>
    get<K.Comparison>("/analyses/compare", { baseline_id: baselineId, scenario_id: scenarioId, limit, offset }, s),
};
