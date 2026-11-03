import { call, type Params } from "./registryApi";
import type * as K from "./impactTypes";

const P = "/impact";
const get = <T>(path: string, params?: Params, signal?: AbortSignal) => call<T>("GET", path, { prefix: P, params, signal });

export const impactApi = {
  limits: (s?: AbortSignal) => get<K.ImpactLimits>("/limits", undefined, s),
  list: (limit: number, offset: number, s?: AbortSignal) => get<K.Page<K.Analysis>>("/analyses", { limit, offset }, s),
  get: (id: number, s?: AbortSignal) => get<K.Analysis>(`/analyses/${id}`, undefined, s),
  start: (runId: number) =>
    call<K.Analysis>("POST", "/analyses", { prefix: P, body: { run_id: runId } }),
  cancel: (id: number) => call<K.Analysis>("POST", `/analyses/${id}/cancel`, { prefix: P, body: {} }),
  summary: (id: number, s?: AbortSignal) => get<K.SummaryResponse>(`/analyses/${id}/summary`, undefined, s),
  events: (id: number, s?: AbortSignal) => get<{ items: K.EventItem[]; total: number }>(`/analyses/${id}/events`, undefined, s),
  findings: (id: number, f: K.FindingFilters, s?: AbortSignal) => get<K.Page<K.Finding>>(`/analyses/${id}/findings`, { ...f }, s),
  finding: (id: number, type: string, entityId: number, s?: AbortSignal) => get<K.FindingDetail>(`/analyses/${id}/findings/${type}/${entityId}`, undefined, s),
  criticality: (id: number, limit: number, offset: number, bottleneckOnly: boolean, s?: AbortSignal) =>
    get<K.Page<K.Criticality> & { note: string }>(`/analyses/${id}/criticality`, { limit, offset, bottleneck_only: bottleneckOnly }, s),
  alternatives: (id: number, limit: number, offset: number, s?: AbortSignal) =>
    get<K.Page<K.Alternative> & { notice: string }>(`/analyses/${id}/alternatives`, { limit, offset }, s),
  graph: (id: number, maxNodes: number, s?: AbortSignal) => get<K.GraphView>(`/analyses/${id}/graph`, { max_nodes: maxNodes }, s),
  compare: (ids: number[], s?: AbortSignal) => get<K.Comparison>("/analyses/compare", { ids }, s),
};
