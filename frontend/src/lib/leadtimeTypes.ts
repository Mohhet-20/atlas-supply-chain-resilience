// Types for the Lead-Time & Delivery Risk API. All example data in tests is SYNTHETIC.
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
export type Unit = "HOURS" | "CALENDAR_DAYS" | "BUSINESS_DAYS";
export type Definition = "ORDER_TO_RECEIPT" | "DISPATCH_TO_ARRIVAL" | "ORDER_TO_DISPATCH" | "PROMISE_DEVIATION";
export type PredictionPoint = "ORDER_PLACEMENT" | "CONFIRMATION" | "DISPATCH";

export interface LeadtimeLimits {
  algorithm_version: string;
  max_import_rows: number;
  max_records_per_run: number;
  max_active_runs: number;
  time_budget_seconds: number;
  definitions: Definition[];
  write_enabled: boolean;
  statements: Record<string, string>;
  tenancy: string;
}
export interface LeadBlock {
  n: number;
  mean: number | null;
  median: number | null;
  std: number | null;
  min: number | null;
  max: number | null;
  percentiles: Record<string, number | null>;
  percentiles_unsupported: Record<string, string>;
  note_max: string | null;
}
export interface PunctBlock {
  commitment: string;
  grace_hours: number;
  n: number;
  n_missing_commitment: number;
  on_time: number;
  late: number;
  on_time_rate: number | null;
  late_rate: number | null;
  late_rate_ci95: [number, number] | null;
  mean_delay_when_late: number | null;
  note: string;
}
export interface Summary {
  definition: Definition;
  unit: Unit;
  description: string;
  as_of: string;
  counts: { deliveries: number; by_state: Record<string, number>; eligible_completed: number; with_lead_time: number; censored: number; excluded_by_reason: Record<string, number> };
  lead_time: LeadBlock;
  punctuality: PunctBlock;
  freshness: { latest_outcome_at: string | null; age_days: number | null };
  min_sample: number;
  sufficient: boolean;
  warnings: string[];
  statements: string[];
}
export interface GroupRow {
  key: (string | number | null)[];
  deliveries: number;
  censored: number;
  n_lead_time: number;
  median_lead_time: number | null;
  p90_lead_time: number | null;
  n_punctuality: number;
  late: number;
  late_rate: number | null;
  late_rate_ci95: [number, number] | null;
  sufficient_sample: boolean;
  mean_delay_when_late: number | null;
}
export interface Groups {
  group_by: string;
  min_sample: number;
  overall: { late_rate: number | null; n_punctuality: number };
  groups: GroupRow[];
  ranking_note: string;
}
export interface Trend {
  window_days: number;
  status: string;
  min_sample: number;
  distinguishable: boolean | null;
  recent: { n: number; late_rate: number | null; late_rate_ci95: [number, number] | null };
  history: { n: number; late_rate: number | null; late_rate_ci95: [number, number] | null };
  note?: string;
}
export interface DeliveryRow {
  delivery_ref: string;
  product_id: number;
  supplier_id: number | null;
  destination_facility_id: number;
  state: string;
  state_reason: string | null;
  order_at: string | null;
  original_promised_at: string | null;
  latest_promised_at: string | null;
  arrived_at: string | null;
  quantity: number | null;
  unit: string | null;
  transport_mode: string | null;
  flags: string[];
}
export interface ImportBatch {
  id: number;
  batch_uid: string;
  kind: string;
  status: string;
  source_label: string;
  rows_total: number;
  rows_accepted: number;
  rows_duplicate: number;
  rows_rejected: number;
  created_at: string;
}
export interface ImportPreview {
  mode: "preview";
  would_write: number;
  summary: { rows_total: number; rows_accepted: number; rows_rejected: number; rows_duplicate?: number };
  rows: { row_number: number; outcome: string; errors: { code: string; message: string }[]; warnings?: { code: string; message: string }[] }[];
}
export interface ImportCommit {
  committed: boolean;
  created: boolean;
  replayed_existing_batch: boolean;
  batch: ImportBatch;
}
export interface RunSummary {
  id: number;
  run_uid: string;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "PARTIAL" | "FAILED" | "CANCELLED";
  result_status: string | null;
  target: "LEAD_TIME" | "LATE_RISK";
  selected_model: string | null;
  selection_status: string | null;
  score_kind: string | null;
  queued_at: string;
  error: string | null;
  statement: string;
}
export type Json = Record<string, unknown>;
export interface RunDetail extends RunSummary {
  config: Json;
  artifact: Json | null;
  splits: Json | null;
  candidates: { evaluated?: string[]; ineligible?: Record<string, string>; failed?: Record<string, string> } | null;
  evaluation: { models?: Record<string, { validation?: Json; test?: Json }> } | null;
  selection: { status?: string; selected?: string; rationale?: string } | null;
  calibration: Json | null;
  intervals: Json | null;
  explanation: Json | null;
  leakage_controls: unknown;
  warnings: string[] | null;
  failures: { model: string; reason: string }[] | null;
  statements: Record<string, string>;
}
export interface Assessment {
  id: number;
  assessment_uid: string;
  subject: "DELIVERY" | "LANE";
  delivery_ref: string | null;
  product_id: number;
  supplier_id: number | null;
  destination_facility_id: number;
  prediction_point: PredictionPoint;
  as_of: string;
  status: "ASSESSED" | "UNAVAILABLE";
  unavailable_reason: string | null;
  estimate: { value: number; unit: string; method: string; method_kind: string; n: number; interval: { lower: number; upper: number; level: number } | null; statement: string } | null;
  arrival: { status: string; estimated_arrival?: string; window?: { earliest: string; latest: string; level: number }; potential_delay_hours?: number; reason?: string } | null;
  risk: { kind: string; score: number | null; band?: string; historical_late_rate?: { late_rate?: number | null; n?: number } | null; statement: string; note?: string } | null;
  factors?: { feature: string; contribution: number; unit: string }[] | null;
  warnings?: string[] | null;
  statement: string;
  created_at: string;
}
export interface Reliability {
  supplier_id: number;
  summary: Summary;
  supplier_risk_delivery_factor: { assessment_uid: string; state: string; score: number | null; explanation: string } | null;
  note: string;
}
