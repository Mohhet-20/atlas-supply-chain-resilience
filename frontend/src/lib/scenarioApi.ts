import { call, type Params } from "./registryApi";
import type * as K from "./scenarioTypes";

const P = "/scenarios";
const get = <T>(path: string, params?: Params, signal?: AbortSignal) => call<T>("GET", path, { prefix: P, params, signal });
const send = <T>(method: "POST" | "PATCH" | "DELETE", path: string, body?: unknown, params?: Params) =>
  call<T>(method, path, { prefix: P, body, params });

export const scenarioApi = {
  limits: (s?: AbortSignal) => get<K.Limits>("/limits", undefined, s),
  templates: (s?: AbortSignal) => get<{ templates: K.Template[]; disclaimer: string }>("/templates", undefined, s),
  list: (f: K.ScenarioFilters, s?: AbortSignal) => get<K.Page<K.Scenario>>("", { ...f }, s),
  get: (id: number, s?: AbortSignal) => get<K.Scenario>(`/${id}`, undefined, s),
  create: (body: { name: string; scenario_type: K.ScenarioType; description?: string; template_key?: string; assumptions?: Partial<K.Assumptions> }) =>
    send<K.Scenario>("POST", "", body),
  update: (id: number, body: Record<string, unknown> & { expected_version: number }) => send<K.Scenario>("PATCH", `/${id}`, body),
  archive: (id: number, version: number) => send<K.Scenario>("POST", `/${id}/archive`, undefined, { expected_version: version }),
  restore: (id: number) => send<K.Scenario>("POST", `/${id}/restore`, {}),
  addEvent: (id: number, body: K.EventBody, version: number) =>
    send<{ event: K.ScenarioEvent; scenario_version: number; scenario_status: K.ScenarioStatus }>("POST", `/${id}/events`, body, { expected_version: version }),
  updateEvent: (id: number, eventId: number, body: Partial<K.EventBody>, version: number) =>
    send<{ event: K.ScenarioEvent; scenario_version: number }>("PATCH", `/${id}/events/${eventId}`, body, { expected_version: version }),
  removeEvent: (id: number, eventId: number, version: number) =>
    send<{ removed: string; scenario_version: number }>("DELETE", `/${id}/events/${eventId}`, undefined, { expected_version: version }),
  validate: (id: number) => send<K.ValidationReport>("POST", `/${id}/validate`, {}),
  startRun: (id: number) => send<K.Run>("POST", `/${id}/runs`, {}),
  scenarioRuns: (id: number, limit: number, offset: number, s?: AbortSignal) => get<K.Page<K.Run>>(`/${id}/runs`, { limit, offset }, s),
  runs: (limit: number, offset: number, s?: AbortSignal) => get<K.Page<K.Run>>("/runs", { limit, offset }, s),
  run: (id: number, s?: AbortSignal) => get<K.Run>(`/runs/${id}`, undefined, s),
  cancelRun: (id: number) => send<K.Run>("POST", `/runs/${id}/cancel`, {}),
  impacts: (id: number, p: { limit: number; offset: number; impact_kind?: string; exposure_level?: string; q?: string; sort?: string }, s?: AbortSignal) =>
    get<K.ImpactPage>(`/runs/${id}/impacts`, { ...p }, s),
  impact: (id: number, type: K.EntityKind, entityId: number, s?: AbortSignal) => get<K.ImpactDetail>(`/runs/${id}/impacts/${type}/${entityId}`, undefined, s),
  edges: (id: number, limit: number, offset: number, s?: AbortSignal) => get<K.Page<K.EdgeImpact>>(`/runs/${id}/edges`, { limit, offset }, s),
  alternatives: (id: number, p: { limit: number; offset: number; for_type?: string; for_id?: number }, s?: AbortSignal) =>
    get<K.Page<K.Alternative> & { notice: string }>(`/runs/${id}/alternatives`, { ...p }, s),
  compare: (ids: number[], s?: AbortSignal) => get<K.Comparison>("/compare", { run_ids: ids }, s),
};
