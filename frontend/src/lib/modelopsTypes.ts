// Mirrors /api/v1/modelops. Every value comes from the backend; nothing here is computed in the browser.
export type ModelState = "DEVELOPMENT" | "EVALUATED" | "APPROVED" | "DEPRECATED" | "ARCHIVED";
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
export interface ModelVersion {
  model_uid: string;
  model_key: string;
  version: string;
  family: string;
  task: string;
  domain: string;
  target: string;
  lifecycle_state: ModelState;
  state_changed_at: string;
  source_type: string;
  source_ref: string;
  algorithm: string;
  artifact_ref: string;
  created_by: string;
  created_at: string;
}
export interface ModelDetail extends ModelVersion {
  intended_use: string[];
  prohibited_use: string[];
  limitations: string[];
  operating_conditions: Record<string, unknown> | null;
  feature_schema: Record<string, unknown> | null;
  preprocessing_version: string | null;
  data_snapshot_hash: string | null;
  training_window: unknown;
  evaluation_window: unknown;
  hyperparameters: Record<string, unknown> | null;
  seed: number | null;
  artifact_checksum: string | null;
  code_revision: string;
  dependency_versions: Record<string, string>;
  fallback_behavior: string | null;
  known_issues: string[];
  monitoring_requirements: string[];
  statement: string;
}
export interface Metric {
  value: number | null;
  n: number;
  status: "ok" | "undefined" | "insufficient_data";
  note: string | null;
}
export interface Check {
  check: string;
  status: "pass" | "fail" | "not_checked";
  detail: string;
}
export interface EvaluationSummary {
  eval_uid: string;
  kind: "RECORDED_BACKTEST" | "PROSPECTIVE";
  status: string;
  result_status: string | null;
  requested_by: string;
  algorithm_version: string;
  config: Record<string, unknown>;
  config_hash: string;
  queued_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  error: string | null;
  validity_overall: string | null;
}
export interface EvaluationDetail extends EvaluationSummary {
  dataset_ref: Record<string, unknown> | null;
  split: Record<string, unknown> | null;
  sample: Record<string, unknown> | null;
  metrics: Record<string, unknown> | null;
  baselines: { entries?: Record<string, unknown>; note?: string; verdict?: Record<string, unknown> | null } | null;
  folds: Record<string, unknown>[] | null;
  segments: Record<string, unknown> | null;
  calibration: Record<string, unknown> | null;
  intervals: Record<string, unknown> | null;
  validity: { overall: string; checks: Check[] } | null;
  warnings: string[] | null;
  limitations: string[] | null;
  reproducibility: Record<string, unknown> | null;
  software: Record<string, unknown> | null;
  statements: string[];
}
export interface LifecycleItem {
  from_state: string | null;
  to_state: string;
  actor: string;
  note: string | null;
  evidence: Record<string, unknown> | null;
  created_at: string;
}
export interface Lifecycle {
  model_uid: string;
  state: ModelState;
  allowed_transitions: { to: string; minimum_role: string }[];
  items: LifecycleItem[];
  statement: string;
}
export interface Explanation {
  status: "ok" | "unsupported";
  kind?: string;
  method: string;
  reason?: string;
  supported_methods?: string[];
  model_version: string;
  statement: string;
  cached?: boolean;
  [k: string]: unknown;
}
export interface Diagnostic {
  id: number;
  check_uid: string;
  kind: string;
  feature: string | null;
  status: string;
  severity: string;
  statistic: Record<string, unknown> | null;
  explanation: string;
  created_at: string;
}
export interface CheckResult {
  check_uid: string;
  overall: string;
  statement: string;
  diagnostics: Diagnostic[];
}
export interface Compare {
  items: (ModelVersion & { evaluation: { eval_uid: string; kind: string; metrics: Record<string, Metric> | null; sample: Record<string, unknown> | null; validity: string | null } | null })[];
  comparable: boolean;
  warnings: string[];
  note: string;
}
export interface Health {
  algorithm_version: string;
  write_api_enabled: boolean;
  registered_versions: number;
  by_state: Record<string, number>;
  not_supported: string[];
  statements: Record<string, string>;
  explanation_methods: { supported: string[]; unsupported: Record<string, string> };
  metric_definitions: Record<string, { definition: string; direction: string }>;
}
export interface Card {
  statement: string;
  identity: Record<string, string>;
  intended_use: string[] | string;
  prohibited_use: string[] | string;
  limitations: string[] | string;
  training: Record<string, unknown>;
  reproducibility: Record<string, unknown>;
  evaluation: { recorded_backtest: unknown; prospective: unknown; missing: string[] };
  monitoring: { requirements: unknown; open_signals: { kind: string; feature: string | null; status: string; explanation: string }[]; note: string };
  governance: { state: string; history: { from: string | null; to: string; actor: string; note: string | null; at: string }[] };
  statements: Record<string, string>;
}
