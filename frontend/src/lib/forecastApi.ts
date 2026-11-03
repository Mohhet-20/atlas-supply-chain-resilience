import { call, type Params } from "./registryApi";
import type * as F from "./forecastTypes";

const P = "/forecast";
const get = <T>(path: string, params?: Params, signal?: AbortSignal) => call<T>("GET", path, { prefix: P, params, signal });

export interface SeriesQuery {
  limit: number;
  offset: number;
  q?: string;
}
export interface DiagQuery {
  product_id: number;
  facility_id: number;
  frequency?: F.Frequency;
  lookback_periods?: number;
}

export const forecastApi = {
  limits: (s?: AbortSignal) => get<F.ForecastLimits>("/limits", undefined, s),
  models: (s?: AbortSignal) => get<{ items: F.ModelInfo[] }>("/models", undefined, s),
  validate: (config: Partial<F.ForecastConfig>) => call<F.ConfigValidation>("POST", "/config/validate", { prefix: P, body: { config } }),
  series: (p: SeriesQuery, s?: AbortSignal) => get<F.Page<F.SeriesItem>>("/series", { ...p }, s),
  diagnostics: (p: DiagQuery, s?: AbortSignal) => get<F.Diagnostics>("/series/diagnostics", { ...p }, s),
  preview: (product_id: number, facility_id: number, config: Partial<F.ForecastConfig>) =>
    call<F.PreviewResponse>("POST", "/candidates/preview", { prefix: P, body: { product_id, facility_id, config } }),
  runs: (p: { limit: number; offset: number; product_id?: number; facility_id?: number; status?: string }, s?: AbortSignal) => get<F.Page<F.RunSummary>>("/runs", { ...p }, s),
  run: (id: number, s?: AbortSignal) => get<F.RunDetail>(`/runs/${id}`, undefined, s),
  start: (product_id: number, facility_id: number, config: Partial<F.ForecastConfig>) =>
    call<F.RunSummary>("POST", "/runs", { prefix: P, body: { product_id, facility_id, config } }),
  cancel: (id: number) => call<F.RunSummary>("POST", `/runs/${id}/cancel`, { prefix: P, body: {} }),
  values: (id: number, s?: AbortSignal) => get<F.ValuesResponse>(`/runs/${id}/values`, undefined, s),
  intervals: (id: number, s?: AbortSignal) => get<F.IntervalsResponse>(`/runs/${id}/intervals`, undefined, s),
  metrics: (id: number, s?: AbortSignal) => get<{ models: Record<string, F.ModelEvaluation>; ranking: { model: string; score: number }[] | null; selected: string | null }>(`/runs/${id}/metrics`, undefined, s),
  vsActual: (id: number, s?: AbortSignal) => get<F.ForecastVsActual>(`/runs/${id}/forecast-vs-actual`, undefined, s),
  alerts: (limit: number, offset: number, s?: AbortSignal) => get<F.Page<F.Alert>>("/alerts", { limit, offset }, s),
  check: () => call<{ count: number; note: string }>("POST", "/monitoring/check", { prefix: P, body: {} }),
};
