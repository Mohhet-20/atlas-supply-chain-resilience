import { call, type Params } from "./registryApi";
import type * as T from "./transportTypes";

const P = "/routing";
const get = <X>(path: string, params?: Params, signal?: AbortSignal) => call<X>("GET", path, { prefix: P, params, signal });

export type RouteRequestBody = Record<string, unknown>;

export const transportApi = {
  health: (s?: AbortSignal) => get<T.Health>("/health", undefined, s),
  objectives: (s?: AbortSignal) => get<T.ObjectivesDoc>("/objectives", undefined, s),
  constraints: (s?: AbortSignal) => get<T.ConstraintsDoc>("/constraints", undefined, s),
  network: (s?: AbortSignal) => get<T.NetworkView>("/network", undefined, s),
  lanes: (p: { limit: number; offset: number; mode?: string; status?: string }, s?: AbortSignal) => get<T.Page<T.LaneRecord>>("/lanes", { ...p }, s),
  runs: (p: { limit: number; offset: number; status?: string; kind?: string }, s?: AbortSignal) => get<T.Page<T.RunListItem>>("/runs", { ...p }, s),
  run: (uid: string, s?: AbortSignal) => get<T.RunDetail>(`/runs/${uid}`, undefined, s),
  optimize: (body: RouteRequestBody) => call<T.RunListItem>("POST", "/optimize", { prefix: P, body }),
  cancel: (uid: string) => call<T.RunListItem>("POST", `/runs/${uid}/cancel`, { prefix: P }),
};
