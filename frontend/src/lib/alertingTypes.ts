export type Severity = "INFO" | "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type AlertStatus = "open" | "acknowledged" | "investigating" | "escalated" | "resolved" | "closed" | "suppressed";
export type Basis = "observed" | "predicted" | "assessed" | "hypothetical";

export interface Page<T> { items: T[]; total: number; limit: number; offset: number }

export interface DetectorInfo {
  key: string; label: string; source: string; basis: Basis; mode: string; available: boolean; meaning: string; missing: string; unavailable_reason: string | null;
  params: Record<string, { type?: string; default?: unknown; min?: number; max?: number; choices?: string[]; unit?: string; required?: boolean }>;
}
export interface Health {
  engine_version: string; event_schema_version: string; severities: Severity[]; transitions: Record<string, string[]>; detectors: DetectorInfo[];
  event_types: Record<string, { label: string; required: Record<string, string>; optional: Record<string, string> }>;
  channels: Record<string, { available: boolean; configured?: string[]; reason?: string | null }>;
  limits: Record<string, number>; tick_seconds: number; retry: { max_attempts: number; base_seconds: number }; not_supported: string[]; scope: string; live_alerts: number;
}

export interface AlertRow {
  alert_uid: string; title: string; summary: string | null; source: string; detector: string; basis: Basis; severity: Severity; priority: string; status: AlertStatus;
  entity_type: string | null; entity_id: string | null; entity_label: string | null; link: string | null; assignee: string | null; rule_uid: string | null; rule_name: string | null;
  rule_version: number | null; last_rule_version: number | null; first_seen_at: string; last_seen_at: string; occurrence_count: number;
  due_ack_at: string | null; due_resolve_at: string | null; escalation_level: number; acknowledged_at: string | null; acknowledged_by: string | null;
  resolved_at: string | null; resolution_mode: string | null; reopened_count: number; version: number;
}
export interface Delivery {
  delivery_uid: string; channel: string; recipient: string; kind: string; status: string; attempts: number; max_attempts: number; next_attempt_at: string | null;
  last_error: string | null; created_at: string; attempt_log: { n: number; at: string; outcome: string; detail: string | null }[];
}
export interface AlertDetail extends AlertRow {
  rule_definition_at_creation: Record<string, unknown> | null; can_transition_to: string[];
  occurrences: { observed_at: string; value: number | null; event_id: number | null; detail: unknown }[];
  transitions: { from: string | null; to: string; actor: string; mode: string; reason: string | null; at: string }[];
  notes: { kind: string; text: string; author: string; at: string }[];
  escalations: { level: number; to: string; from: string | null; trigger: string; actor: string; reason: string | null; at: string }[];
  related: { alert_uid: string; title: string; status: string; severity: string; reason: string | null; linked_by: string }[];
  suppression: { reason: string; expires_at: string | null } | null;
  deliveries: Delivery[] | null;
}
export interface RuleView {
  rule_uid: string; name: string; description?: string | null; detector: string; severity: Severity; enabled: boolean; params: Record<string, unknown>;
  notify: { channels?: string[]; recipients?: string[]; min_severity?: string; webhook?: string | null };
  escalation_policy?: string | null; version: number; created_by: string; created_at: string; updated_at: string; available: boolean;
}
export interface RuleDraft {
  name: string; detector: string; severity: Severity; params: Record<string, unknown>;
  notify: { channels: string[]; recipients: string[]; min_severity: Severity }; escalation_policy?: string | null;
}
export interface Policy { policy_uid: string; name: string; enabled: boolean; ack_minutes: number; resolve_minutes: number | null; escalate_after_minutes: number; targets: string[]; version: number; created_by: string }
export interface Inbox {
  id: number; alert_uid: string; title: string; severity: Severity; source: string; kind: string; body: string; created_at: string; read: boolean; read_at: string | null;
}
export interface Prefs {
  in_app_enabled: boolean; min_severity: Severity; quiet_start_hour: number | null; quiet_end_hour: number | null; timezone: string;
  channels: Record<string, { available: boolean; reason?: string | null }>; note: string;
}
export interface Dist { n: number; mean_minutes: number | null; median_minutes: number | null }
export interface SlaTally { met: number; missed: number; not_yet_due: number; met_pct: number | null }
export interface AlertAnalytics {
  window_days: number; alerts_in_window: number; definitions: Record<string, string>;
  by_severity: Record<string, number>; by_source: Record<string, number>; by_basis: Record<string, number>; by_state_now: Record<string, number>;
  acknowledgement: Dist & { unacknowledged: number }; resolution: Dist & { unresolved: number };
  sla: { acknowledgement: SlaTally; resolution: SlaTally };
  unresolved_age: Record<string, number | null>;
  [k: string]: unknown;
}
export interface SlaItem { alert_uid: string; title: string; severity: Severity; status: string; target: string; due_at: string; state: "overdue" | "due_soon" | "on_track"; remaining_minutes: number; elapsed_minutes: number; priority: string; assignee: string | null }
export interface SlaView { items: SlaItem[]; note: string; counts: { overdue: number; due_soon: number; on_track: number } }
export interface TickResult { escalated: number; unsuppressed: number; [k: string]: unknown }
