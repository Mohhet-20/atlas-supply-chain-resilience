import { call, type Params } from "./registryApi";
import type * as T from "./decisionTypes";

const P = "/decision";
const get = <X>(path: string, params?: Params, signal?: AbortSignal) => call<X>("GET", path, { prefix: P, params, signal });
const post = <X>(path: string, body?: unknown) => call<X>("POST", path, { prefix: P, body: body ?? {} });

export const decisionApi = {
  health: (s?: AbortSignal) => get<T.Health>("/health", undefined, s),
  metrics: (s?: AbortSignal) => get<T.Metrics>("/metrics", undefined, s),
  candidates: (kind?: T.EvidenceKind, s?: AbortSignal) => get<{ items: T.Candidate[]; note: string }>("/evidence/candidates", { kind, limit: 100 }, s),
  validate: (def: unknown) => post<T.ValidationReport>("/cases/validate", def),
  create: (def: unknown) => post<T.CaseDetail>("/cases", def),
  cases: (p: { limit: number; offset: number; status?: string; q?: string; reviewer?: string; sort?: string }, s?: AbortSignal) => get<T.Page<T.CaseItem>>("/cases", { ...p }, s),
  case: (uid: string, s?: AbortSignal) => get<T.CaseDetail>(`/cases/${uid}`, undefined, s),
  edit: (uid: string, expected_version: number, definition: unknown) => call<T.CaseDetail>("PUT", `/cases/${uid}`, { prefix: P, body: { expected_version, definition } }),
  clone: (uid: string) => post<T.CaseDetail>(`/cases/${uid}/clone`),
  ready: (uid: string) => post<{ case: T.CaseDetail; validation: T.ValidationReport }>(`/cases/${uid}/ready`),
  evaluate: (uid: string) => post<T.EvalOutcome>(`/cases/${uid}/evaluate`),
  evaluations: (uid: string, s?: AbortSignal) => get<T.Page<T.EvalSummary>>(`/cases/${uid}/evaluations`, { limit: 50 }, s),
  evaluation: (uid: string, eid: string, s?: AbortSignal) => get<T.Evaluation>(`/cases/${uid}/evaluations/${eid}`, undefined, s),
  replay: (uid: string, eid: string) => post<T.Replay>(`/cases/${uid}/evaluations/${eid}/replay`),
  reviewer: (uid: string, reviewer: string) => post<T.CaseDetail>(`/cases/${uid}/reviewer`, { reviewer }),
  note: (uid: string, kind: "COMMENT" | "CONCERN" | "ASSUMPTION", text: string) => post<T.CaseEvent>(`/cases/${uid}/notes`, { kind, text }),
  submit: (uid: string) => post<T.CaseDetail>(`/cases/${uid}/submit`),
  returnCase: (uid: string, text: string) => post<T.CaseDetail>(`/cases/${uid}/return`, { text }),
  decide: (uid: string, decision: "APPROVED" | "REJECTED", rationale: string, alternative_key?: string) =>
    post<T.CaseDetail>(`/cases/${uid}/decision`, { decision, rationale, ...(alternative_key ? { alternative_key } : {}) }),
  supersede: (uid: string, text: string) => post<T.CaseDetail>(`/cases/${uid}/supersede`, { text }),
  events: (uid: string, p: { limit: number; offset: number }, s?: AbortSignal) => get<T.Page<T.CaseEvent>>(`/cases/${uid}/events`, { ...p }, s),
  versions: (uid: string, s?: AbortSignal) => get<T.Page<T.VersionItem>>(`/cases/${uid}/versions`, { limit: 50 }, s),
};
