import { call, type Params } from "./registryApi";
import type * as T from "./sourcingTypes";

const P = "/sourcing";
const get = <X>(path: string, params?: Params, signal?: AbortSignal) => call<X>("GET", path, { prefix: P, params, signal });
export type Body = Record<string, unknown>;

export const sourcingApi = {
  health: (s?: AbortSignal) => get<T.Health>("/health", undefined, s),
  offers: (p: { limit: number; offset: number; product_id?: number; supplier_id?: number; status?: string; approval?: string }, s?: AbortSignal) => get<T.Page<T.Offer>>("/offers", { ...p }, s),
  setOfferStatus: (uid: string, to: string) => call<T.Offer>("POST", `/offers/${uid}/status`, { prefix: P, body: { to } }),
  alternatives: (body: Body) => call<T.Alternatives>("POST", "/alternatives", { prefix: P, body }),
  validate: (body: Body) => call<T.Validation>("POST", "/validate", { prefix: P, body }),
  optimize: (body: Body, key: string) => call<T.RunListItem>("POST", "/optimize", { prefix: P, body, headers: { "Idempotency-Key": key } }),
  compareStrategies: (body: Body, key: string) => call<T.RunListItem>("POST", "/compare-strategies", { prefix: P, body, headers: { "Idempotency-Key": key } }),
  runs: (p: { limit: number; offset: number; result_status?: string; kind?: string }, s?: AbortSignal) => get<T.Page<T.RunListItem>>("/runs", { ...p }, s),
  run: (uid: string, s?: AbortSignal) => get<T.RunDetail>(`/runs/${uid}`, undefined, s),
  cancel: (uid: string) => call<T.RunListItem>("POST", `/runs/${uid}/cancel`, { prefix: P }),
  decisions: (uid: string, s?: AbortSignal) => get<T.DecisionHistory>(`/runs/${uid}/decisions`, undefined, s),
  decide: (uid: string, decision: "APPROVED" | "REJECTED", rationale: string) => call<T.RunDetail>("POST", `/runs/${uid}/decision`, { prefix: P, body: { decision, rationale } }),
};
