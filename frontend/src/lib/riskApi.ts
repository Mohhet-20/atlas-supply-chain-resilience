import { call, type Params } from "./registryApi";
import type * as K from "./riskTypes";

const get = <T>(path: string, params?: Params, signal?: AbortSignal) => call<T>("GET", path, { prefix: "/risk", params, signal });
const post = <T>(path: string, body: unknown) => call<T>("POST", path, { prefix: "/risk", body });

export const riskApi = {
  methodology: (s?: AbortSignal) => get<K.Methodology>("/methodology", undefined, s),
  overview: (s?: AbortSignal) => get<K.Overview>("/overview", undefined, s),
  geography: (s?: AbortSignal) => get<K.Geography>("/geography", undefined, s),
  suppliers: (f: K.SupplierFilters, s?: AbortSignal) => get<K.SupplierPage>("/suppliers", { ...f }, s),
  supplier: (id: number, s?: AbortSignal) => get<K.SupplierRisk>(`/suppliers/${id}`, undefined, s),
  history: (id: number, limit: number, offset: number, s?: AbortSignal) => get<K.HistoryPage>(`/suppliers/${id}/assessments`, { limit, offset }, s),
  dependencies: (id: number, s?: AbortSignal) => get<K.Dependencies>(`/suppliers/${id}/dependencies`, undefined, s),
  compare: (ids: number[], s?: AbortSignal) => get<K.Comparison>("/compare", { ids }, s),
  alerts: (p: { status?: string; alert_type?: string; limit: number; offset: number }, s?: AbortSignal) => get<K.AlertPage>("/alerts", { ...p }, s),
  transitionAlert: (id: number, to: K.AlertStatus, version: number, note?: string) =>
    post<K.Alert>(`/alerts/${id}/transition`, { to, version, note: note || undefined }),
  runs: (limit: number, offset: number, s?: AbortSignal) => get<K.RunPage>("/runs", { limit, offset }, s),
  run: (id: number, s?: AbortSignal) => get<K.Run>(`/runs/${id}`, undefined, s),
  startRun: (body: { scope: K.RunScope; supplier_id?: number; supplier_ids?: number[]; reason?: string }) => post<K.Run>("/runs", body),
  assess: (id: number) => post<{ run: K.Run; result: K.SupplierRisk }>(`/suppliers/${id}/assess`, {}),
  review: (id: number, body: K.ReviewBody) => post<K.Review>(`/suppliers/${id}/reviews`, body),
};

export const supplierRiskPath = (id: number) => `/risk/suppliers/${id}`;
