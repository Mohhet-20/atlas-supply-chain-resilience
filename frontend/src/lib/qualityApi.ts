import { call, type Params } from "./registryApi";
import type * as Q from "./qualityTypes";

const get = <T>(path: string, params?: Params, signal?: AbortSignal) => call<T>("GET", path, { prefix: "/quality", params, signal });
const post = <T>(path: string, body: unknown) => call<T>("POST", path, { prefix: "/quality", body });

export const qualityApi = {
  overview: (s?: AbortSignal) => get<Q.Overview>("/overview", undefined, s),
  analytics: (s?: AbortSignal) => get<Q.Analytics>("/analytics", undefined, s),
  sources: (s?: AbortSignal) => get<{ items: Q.SourceReport[] }>("/sources", undefined, s),
  rules: (s?: AbortSignal) => get<{ items: Q.RuleInfo[]; formula_version: string }>("/rules", undefined, s),
  setRule: (id: string, enabled: boolean, reason: string) =>
    call<{ rule_id: string; enabled: boolean; note: string }>("PATCH", `/rules/${id}`, { prefix: "/quality", body: { enabled, reason } }),
  findings: (f: Q.FindingFilters, s?: AbortSignal) => get<Q.FindingPage>("/findings", { ...f }, s),
  finding: (id: number, s?: AbortSignal) => get<Q.FindingDetail>(`/findings/${id}`, undefined, s),
  transition: (id: number, action: Q.FindingAction, expected_version: number, note?: string) =>
    post<Q.Finding>(`/findings/${id}/transition`, { action, expected_version, note: note || undefined }),
  remediate: (id: number, expected_version: number) => post<{ status: string; fields: string[] }>(`/findings/${id}/remediate`, { expected_version, confirm: true }),
  runs: (limit: number, offset: number, s?: AbortSignal) => get<Q.RunPage>("/runs", { limit, offset }, s),
  run: (id: number, s?: AbortSignal) => get<Q.RunInfo>(`/runs/${id}`, undefined, s),
  startRun: (body: Record<string, unknown>) => post<Q.RunInfo>("/runs", body),
  entity: (kind: string, id: number, s?: AbortSignal) => get<Q.EntityQuality>(`/entities/${kind}/${id}`, undefined, s),
  lineage: (kind: string, id: number, s?: AbortSignal) => get<Q.Lineage>(`/lineage/${kind}/${id}`, undefined, s),
  batch: (id: number, classification: string | undefined, offset: number, s?: AbortSignal) =>
    get<Q.BatchReport>(`/batches/${id}`, { classification, limit: 50, offset }, s),
};

/** Relationship refs look like SUPPLY:3 or PRODUCT:5 and map to the quality subject kinds. */
export function edgeSubject(ref: string): { kind: Q.QEntityKind; id: number } | undefined {
  const [prefix, raw] = ref.split(":");
  const id = Number(raw);
  if (!Number.isInteger(id) || id < 1) return undefined;
  if (prefix === "SUPPLY") return { kind: "RELATIONSHIP", id };
  if (prefix === "PRODUCT") return { kind: "PRODUCT_LINK", id };
  return undefined;
}

export const entityPath = (kind: string, id: number) => `/quality/entity/${kind}/${id}`;
