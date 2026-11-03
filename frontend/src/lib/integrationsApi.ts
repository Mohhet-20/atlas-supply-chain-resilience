import { call, type Params as Q } from "./registryApi";
import type * as T from "./integrationsTypes";

const get = <X>(path: string, params?: Q, signal?: AbortSignal) => call<X>("GET", path, { prefix: "", params, signal });
const post = <X>(path: string, body?: unknown) => call<X>("POST", path, { prefix: "", body: body ?? {} });

export interface CreateIntegration { name: string; provider: string; description?: string; config?: Record<string, unknown>; credential_ref?: string; schedule_minutes?: number }

export const integrationsApi = {
  connectors: (s?: AbortSignal) => get<{ items: T.Connector[] }>("/integrations/connectors", undefined, s),
  overview: (s?: AbortSignal) => get<T.Overview>("/integrations/overview", undefined, s),
  list: (q: Q, s?: AbortSignal) => get<T.Page<T.Integration>>("/integrations", q, s),
  get: (uid: string, s?: AbortSignal) => get<T.Integration>(`/integrations/${uid}`, undefined, s),
  create: (b: CreateIntegration) => post<T.Integration>("/integrations", b),
  validate: (uid: string) => post<{ valid: boolean; errors: string[]; status: string }>(`/integrations/${uid}/validate`),
  test: (uid: string) => post<{ ok: boolean; detail: string; status: string }>(`/integrations/${uid}/test`),
  action: (uid: string, a: "enable" | "disable" | "disconnect") => post<T.Integration>(`/integrations/${uid}/${a}`),
  sync: (uid: string, b: { mode: "full" | "incremental"; resume?: boolean }) => post<T.Run>(`/integrations/${uid}/sync`, b),
  runs: (uid: string, q: Q, s?: AbortSignal) => get<T.Page<T.Run>>(`/integrations/${uid}/runs`, q, s),
  run: (runUid: string, s?: AbortSignal) => get<T.RunDetail>(`/integrations/runs/${runUid}`, undefined, s),
  cancel: (runUid: string) => post<T.Run>(`/integrations/runs/${runUid}/cancel`),
  remove: (uid: string) => call<void>("DELETE", `/integrations/${uid}`, { prefix: "" }),
  endpoints: (s?: AbortSignal) => get<T.Page<T.Endpoint>>("/webhooks", { limit: 100 }, s),
  events: (s?: AbortSignal) => get<{ items: T.EventType[] }>("/webhooks/events", undefined, s),
  createEndpoint: (b: { name: string; direction: "outbound" | "inbound"; url?: string; events?: string[] }) => post<T.Endpoint & { secret: string }>("/webhooks", b),
  endpointAction: (uid: string, a: "enable" | "disable" | "test") => post<unknown>(`/webhooks/${uid}/${a}`),
  rotateSecret: (uid: string) => post<T.Endpoint & { secret: string }>(`/webhooks/${uid}/rotate-secret`),
  deliveries: (uid: string, s?: AbortSignal) => get<T.Page<T.Delivery>>(`/webhooks/${uid}/deliveries`, { limit: 25 }, s),
  redeliver: (deliveryUid: string) => post<T.Delivery>(`/webhooks/deliveries/${deliveryUid}/redeliver`),
  accounts: (s?: AbortSignal) => get<T.Page<T.ServiceAccount>>("/service-accounts", { limit: 100 }, s),
  createAccount: (b: { name: string; role: string; description?: string }) => post<T.ServiceAccount>("/service-accounts", b),
  keys: (sa: string, s?: AbortSignal) => get<{ items: T.ApiKey[] }>(`/service-accounts/${sa}/keys`, undefined, s),
  createKey: (sa: string, b: { name: string; ttl_days?: number; scopes?: string[] }) => post<T.ApiKey & { secret: string }>(`/service-accounts/${sa}/keys`, b),
  revokeKey: (keyUid: string) => post<T.ApiKey>(`/service-accounts/keys/${keyUid}/revoke`),
  rotateKey: (keyUid: string) => post<T.ApiKey & { secret: string }>(`/service-accounts/keys/${keyUid}/rotate`),
};
