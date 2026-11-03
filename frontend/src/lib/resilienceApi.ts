import { call, type Params } from "./registryApi";
import type * as T from "./resilienceTypes";

const P = "/resilience";
const get = <X>(path: string, params?: Params, signal?: AbortSignal) => call<X>("GET", path, { prefix: P, params, signal });
export type Body = Record<string, unknown>;

export const resilienceApi = {
  health: (s?: AbortSignal) => get<T.Health>("/health", undefined, s),
  catalogue: (s?: AbortSignal) => get<T.Catalogue>("/catalogue", undefined, s),
  availability: (body: Body) => call<T.Availability>("POST", "/availability", { prefix: P, body }),
  optimize: (body: Body, key: string) => call<T.RunListItem>("POST", "/optimize", { prefix: P, body, headers: { "Idempotency-Key": key } }),
  runs: (p: { limit: number; offset: number; result_status?: string; strategy?: string; status?: string }, s?: AbortSignal) => get<T.Page<T.RunListItem>>("/runs", { ...p }, s),
  run: (uid: string, s?: AbortSignal) => get<T.RunDetail>(`/runs/${uid}`, undefined, s),
  cancel: (uid: string) => call<T.RunListItem>("POST", `/runs/${uid}/cancel`, { prefix: P }),
  sensitivityNow: (uid: string, body: Body) => call<{ sensitivity: T.Sensitivity; note: string }>("POST", `/runs/${uid}/sensitivity`, { prefix: P, body }),
  replay: (uid: string) => call<{ reproduced_from_stored_snapshot: boolean; live_inputs_unchanged: boolean; [k: string]: unknown }>("POST", `/runs/${uid}/replay`, { prefix: P }),
  decisions: (uid: string, s?: AbortSignal) => get<T.DecisionHistory>(`/runs/${uid}/decisions`, undefined, s),
  decide: (uid: string, decision: "APPROVED" | "REJECTED", rationale: string, alternative_id?: string) =>
    call<T.RunDetail>("POST", `/runs/${uid}/decision`, { prefix: P, body: { decision, rationale, ...(alternative_id ? { alternative_id } : {}) } }),
};
