export interface Page<T> { items: T[]; total: number; limit: number; offset: number }

export interface Connector {
  key: string; name: string; category: string; provider: string; description: string;
  status: "implemented" | "sandbox" | "extension_point"; version: string; capabilities: string[];
  needs_credential: boolean; config_fields: Record<string, string>; creatable: boolean;
}
export interface Health {
  state: "healthy" | "degraded" | "unavailable" | "not_configured"; reasons: string[]; recent_runs: number;
  error_rate: number | null; partial_runs: number; avg_duration_ms: number | null; retries: number;
  data_quality_failures: number; last_success_at: string | null; last_failure_at: string | null; last_run_status: string | null;
}
export interface Integration {
  integration_uid: string; name: string; category: string; provider: string; description: string | null;
  config: Record<string, unknown>; credential: { reference: string | null; status: string };
  status: string; enabled: boolean; schedule_minutes: number | null; next_run_at: string | null;
  last_success_at: string | null; last_failure_at: string | null; error_summary: string | null; version: number; health?: Health;
}
export interface Run {
  run_uid: string; mode: string; trigger: string; status: string; started_at: string | null; finished_at: string | null;
  duration_ms: number | null; discovered: number; processed: number; created: number; updated: number; skipped: number;
  rejected: number; failed: number; retries: number; error_summary: string | null; checkpoint: Record<string, unknown>;
}
export interface RunDetail extends Run { errors: { record_ref: string | null; code: string; message: string; at: string | null }[] }
export interface Overview {
  total: number; by_health: Record<string, number>; active_syncs: number; webhook_queue_depth: number;
  webhook_dead_letters: number; sync_errors_recorded: number;
  recent_failures: { run_uid: string; error_summary: string | null; finished_at: string | null }[];
}
export interface Endpoint {
  endpoint_uid: string; name: string; direction: "outbound" | "inbound"; url: string | null; events: string[];
  secret_version: number; status: string; consecutive_failures: number; disabled_reason: string | null; receive_url: string | null;
}
export interface Delivery {
  delivery_uid: string; event_id: string; event_type: string; status: string; attempts: number;
  next_attempt_at: string | null; last_status_code: number | null; last_error: string | null; created_at: string | null; delivered_at: string | null;
}
export interface EventType { event_type: string; version: string; description: string; emitted: boolean }
export interface ServiceAccount { sa_uid: string; name: string; description: string | null; role: string; status: string }
export interface ApiKey {
  key_uid: string; name: string; prefix: string; role: string; scopes: string[]; expires_at: string | null;
  revoked_at: string | null; last_used_at: string | null; use_count: number; rotated_from: string | null;
}
