import { call, type Params } from "./registryApi";
import type * as T from "./invoptTypes";

const P = "/replenishment";
const get = <X>(path: string, params?: Params, signal?: AbortSignal) => call<X>("GET", path, { prefix: P, params, signal });
export type Body = Record<string, unknown>;

export const invoptApi = {
  health: (s?: AbortSignal) => get<T.Health>("/health", undefined, s),
  positions: (p: { limit: number; offset: number; product_id?: number; facility_id?: number }, s?: AbortSignal) => get<T.Positions>("/positions", { ...p }, s),
  costProfiles: (p: { limit: number; offset: number; product_id?: number; facility_id?: number; active?: boolean }, s?: AbortSignal) => get<T.Page<T.CostProfile>>("/cost-profiles", { ...p }, s),
  createCostProfile: (body: Body) => call<T.CostProfile>("POST", "/cost-profiles", { prefix: P, body }),
  retireCostProfile: (uid: string) => call<T.CostProfile>("POST", `/cost-profiles/${uid}/retire`, { prefix: P }),
  calcSafety: (body: Body) => call<T.CalcSafety>("POST", "/calc/safety-stock", { prefix: P, body }),
  calcPolicy: (body: Body) => call<T.CalcPolicy>("POST", "/calc/reorder-point", { prefix: P, body }),
  optimize: (body: Body, key: string) => call<T.RunListItem>("POST", "/optimize", { prefix: P, body, headers: { "Idempotency-Key": key } }),
  simulate: (body: Body, key: string) => call<T.RunListItem>("POST", "/simulate", { prefix: P, body, headers: { "Idempotency-Key": key } }),
  runs: (p: { limit: number; offset: number; result_status?: string; kind?: string }, s?: AbortSignal) => get<T.Page<T.RunListItem>>("/runs", { ...p }, s),
  run: (uid: string, s?: AbortSignal) => get<T.RunDetail>(`/runs/${uid}`, undefined, s),
  cancel: (uid: string) => call<T.RunListItem>("POST", `/runs/${uid}/cancel`, { prefix: P }),
  decisions: (uid: string, s?: AbortSignal) => get<T.DecisionHistory>(`/runs/${uid}/decisions`, undefined, s),
  decide: (uid: string, decision: "APPROVED" | "REJECTED", rationale: string) => call<unknown>("POST", `/runs/${uid}/decision`, { prefix: P, body: { decision, rationale } }),
};
