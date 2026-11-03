import { call, type Params } from "./registryApi";
import type * as M from "./modelopsTypes";

const P = "/modelops";
const get = <T>(path: string, params?: Params, signal?: AbortSignal) => call<T>("GET", path, { prefix: P, params, signal });

export const modelopsApi = {
  health: (s?: AbortSignal) => get<M.Health>("/health", undefined, s),
  models: (p: { limit: number; offset: number; domain?: string; state?: string; q?: string }, s?: AbortSignal) => get<M.Page<M.ModelVersion>>("/models", { ...p }, s),
  model: (uid: string, s?: AbortSignal) => get<M.ModelDetail>(`/models/${uid}`, undefined, s),
  versions: (uid: string, s?: AbortSignal) => get<M.Page<M.ModelVersion>>(`/models/${uid}/versions`, undefined, s),
  lifecycle: (uid: string, s?: AbortSignal) => get<M.Lifecycle>(`/models/${uid}/lifecycle`, undefined, s),
  evaluations: (uid: string, s?: AbortSignal) => get<M.Page<M.EvaluationSummary>>(`/models/${uid}/evaluations`, { limit: 20, offset: 0 }, s),
  evaluation: (uid: string, s?: AbortSignal) => get<M.EvaluationDetail>(`/evaluations/${uid}`, undefined, s),
  explanations: (uid: string, s?: AbortSignal) => get<M.Page<{ id: number; scope: string; subject: string | null; method: string; status: string; created_at: string; result: M.Explanation }>>(`/models/${uid}/explanations`, undefined, s),
  monitoring: (uid: string, s?: AbortSignal) => get<M.Page<M.Diagnostic> & { statement: string }>(`/models/${uid}/monitoring`, { limit: 50, offset: 0 }, s),
  card: (uid: string, s?: AbortSignal) => get<M.Card>(`/models/${uid}/card`, undefined, s),
  compare: (ids: string[], s?: AbortSignal) => get<M.Compare>("/compare", { ids: ids.join(",") }, s),
  sync: () => call<{ registered: number; already_registered: number; not_registrable: number }>("POST", "/models/sync", { prefix: P }),
  evaluate: (uid: string, body: Record<string, unknown>) => call<M.EvaluationSummary>("POST", `/models/${uid}/evaluations`, { prefix: P, body }),
  transition: (uid: string, body: { to_state: M.ModelState; note?: string }) => call<M.ModelVersion>("POST", `/models/${uid}/transition`, { prefix: P, body }),
  explain: (uid: string, body: { scope: "GLOBAL" | "LOCAL"; method: string; subject?: string }) => call<M.Explanation>("POST", `/models/${uid}/explanations`, { prefix: P, body }),
  check: (uid: string) => call<M.CheckResult>("POST", `/models/${uid}/monitoring/checks`, { prefix: P, body: {} }),
};
