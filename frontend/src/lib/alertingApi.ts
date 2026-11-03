import { call, type Params as Q } from "./registryApi";
import type * as T from "./alertingTypes";

const P = "/alerting";
const get = <X>(path: string, params?: Q, signal?: AbortSignal) => call<X>("GET", path, { prefix: P, params, signal });
const post = <X>(path: string, body?: unknown) => call<X>("POST", path, { prefix: P, body: body ?? {} });
const put = <X>(path: string, body: unknown) => call<X>("PUT", path, { prefix: P, body });
export interface ActionInput { expected_version?: number; reason?: string }

export const alertingApi = {
  health: (s?: AbortSignal) => get<T.Health>("/health", undefined, s),
  actors: (s?: AbortSignal) => get<{ users: string[]; teams: string[] }>("/actors", undefined, s),
  alerts: (q: Q, s?: AbortSignal) => get<T.Page<T.AlertRow>>("/alerts", q, s),
  alert: (uid: string, s?: AbortSignal) => get<T.AlertDetail>(`/alerts/${uid}`, undefined, s),
  act: (uid: string, action: "acknowledge" | "investigate" | "resolve" | "close" | "reopen", b: ActionInput) => post<{ alert_uid: string; status: string; version: number }>(`/alerts/${uid}/${action}`, b),
  assign: (uid: string, b: ActionInput & { assignee: string }) => post(`/alerts/${uid}/assign`, b),
  escalate: (uid: string, b: ActionInput & { target?: string }) => post(`/alerts/${uid}/escalate`, b),
  note: (uid: string, b: { kind: "note" | "mitigation"; text: string }) => post(`/alerts/${uid}/notes`, b),
  link: (uid: string, b: { related_alert_uid: string; reason?: string }) => post(`/alerts/${uid}/links`, b),
  rules: (s?: AbortSignal) => get<T.Page<T.RuleView>>("/rules", { limit: 100 }, s),
  validateRule: (b: T.RuleDraft) => post<{ valid: boolean; problems: string[] }>("/rules/validate", b),
  createRule: (b: T.RuleDraft) => post<T.RuleView>("/rules", b),
  toggleRule: (uid: string, state: "enable" | "disable") => post<T.RuleView>(`/rules/${uid}/${state}`),
  policies: (s?: AbortSignal) => get<{ items: T.Policy[] }>("/policies", undefined, s),
  evaluate: () => post<{ created: number; updated: number; rules: unknown[] }>("/evaluate", {}),
  tick: () => post<T.TickResult>("/tick"),
  inbox: (q: Q, s?: AbortSignal) => get<T.Page<T.Inbox>>("/notifications", q, s),
  unread: (s?: AbortSignal) => get<{ unread: number }>("/notifications/count", undefined, s),
  markRead: (id: number) => post(`/notifications/${id}/read`),
  readAll: () => post("/notifications/read-all"),
  prefs: (s?: AbortSignal) => get<T.Prefs>("/preferences", undefined, s),
  savePrefs: (b: Pick<T.Prefs, "in_app_enabled" | "min_severity" | "quiet_start_hour" | "quiet_end_hour">) => put<unknown>("/preferences", b),
  analytics: (days: number, s?: AbortSignal) => get<T.AlertAnalytics>("/analytics", { window_days: String(days) }, s),
  sla: (s?: AbortSignal) => get<T.SlaView>("/sla", undefined, s),
};
