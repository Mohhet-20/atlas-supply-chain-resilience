/** Types for the Inventory & Stockout Intelligence API. Projections are conditional decision support, never forecasts. */
import type { SimStatus } from "./scenarioTypes";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export type ImportKind = "INVENTORY" | "DEMAND" | "REPLENISHMENT";
export type StockStatus = "current_shortage" | "projected_stockout" | "low_coverage" | "within_policy" | "unknown" | "insufficient_data";
export type Confidence = "LOW" | "MEDIUM" | "HIGH";

export interface InventoryLimits {
  max_upload_bytes: number;
  max_import_rows: number;
  max_pairs: number;
  max_horizon_days: number;
  time_budget_seconds: number;
  max_active_analyses: number;
  date_formats: string[];
  fields: Record<ImportKind, { required: string[]; optional: string[] }>;
  demand_kinds: string[];
  receipt_statuses: string[];
  algorithm_version: string;
  disclaimer: string;
}

export interface RowIssue {
  code: string;
  field: string | null;
  message: string;
}
export interface PreviewRow {
  row_number: number;
  outcome: "ACCEPTED" | "DUPLICATE" | "REJECTED";
  errors: RowIssue[];
  warnings: RowIssue[];
  raw: Record<string, unknown> | null;
}
export interface ImportSummary {
  kind: ImportKind;
  rows_total: number;
  rows_accepted: number;
  rows_duplicate: number;
  rows_rejected: number;
  error_codes: Record<string, number>;
  warning_codes: Record<string, number>;
  columns: string[];
  mapping: Record<string, string>;
}
export interface PreviewResponse {
  mode: "preview";
  committed: false;
  summary: ImportSummary;
  rows: PreviewRow[];
  sample_accepted: PreviewRow[];
  would_write: number;
}
export interface ImportBatch {
  id: number;
  batch_uid: string;
  kind: ImportKind;
  status: "COMPLETED" | "REJECTED";
  source_label: string;
  provenance: string;
  file_name: string | null;
  file_format: string;
  requested_by: string;
  mapping: Record<string, string> | null;
  rows_total: number;
  rows_accepted: number;
  rows_duplicate: number;
  rows_rejected: number;
  summary: Record<string, unknown> | null;
  created_at: string;
}
export interface CommitResponse {
  mode: "commit";
  committed: boolean;
  created: boolean;
  replayed_existing_batch: boolean;
  batch: ImportBatch;
}
export interface BatchRow {
  row_number: number;
  outcome: "ACCEPTED" | "DUPLICATE" | "REJECTED";
  errors: RowIssue[] | null;
  raw: Record<string, unknown> | null;
  record_id: number | null;
}

export interface Position {
  id: number;
  product_id: number;
  facility_id: number;
  product_name?: string;
  facility_name?: string;
  observed_at: string;
  unit: string;
  on_hand: number | null;
  reserved: number | null;
  allocated: number | null;
  quarantined: number | null;
  damaged: number | null;
  usable_source: number | null;
  atp_source: number | null;
  in_transit: number | null;
  backordered: number | null;
  provenance: string;
  source_label: string;
  batch_id: number;
}

export interface Quality {
  pairs_with_inventory: number;
  stale_pairs: number;
  max_age_days: number;
  pairs_without_demand: number;
  pairs_without_policy: number;
  import_rows_by_outcome: Record<string, number>;
  top_rejection_codes: [string, number][];
  note: string;
}

export interface Policy {
  id: number;
  product_id: number;
  facility_id: number;
  version: number;
  status: "ACTIVE" | "SUPERSEDED";
  unit: string;
  safety_stock: number | null;
  reorder_point: number | null;
  minimum: number | null;
  maximum: number | null;
  target: number | null;
  review_period_days: number | null;
  lead_time_days: number | null;
  service_level: number | null;
  effective_from: string;
  effective_to: string | null;
  owner: string;
  provenance: string;
  note: string | null;
}
export interface MissingPolicy {
  product_id: number;
  facility_id: number;
  product_name: string | null;
  facility_name: string | null;
}

export interface AnalysisConfig {
  as_of?: string | null;
  horizon_days: number;
  demand_window_days: number;
  max_inventory_age_days: number;
  low_coverage_days: number;
  include_planned_receipts: boolean;
  allocated_overlap: string;
  forecast_run_id?: number | null;
  forecast_conversion_factor?: number | null;
  [k: string]: unknown;
}
export interface Analysis {
  id: number;
  analysis_uid: string;
  kind: "BASELINE" | "SCENARIO";
  status: SimStatus;
  requested_by: string;
  impact_analysis_id: number | null;
  config: AnalysisConfig;
  config_hash: string;
  algorithm_version: string;
  as_of: string;
  input_hash: string | null;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  truncated: boolean;
  truncation_reasons: string[] | null;
  error: string | null;
  disclaimer: string;
  summary: AnalysisSummary | null;
  coverage?: unknown;
  inputs?: Record<string, unknown> | null;
  warnings?: { code: string; text: string }[] | null;
  failures?: unknown[] | null;
}
export interface AnalysisSummary {
  pairs_analysed: number;
  by_status: Partial<Record<StockStatus, number>>;
  kind: "BASELINE" | "SCENARIO";
  current_shortages?: number;
  projected_stockouts?: number;
  low_coverage?: number;
  within_policy?: number;
  unknown?: number;
  insufficient_data?: number;
  stale_inventory?: number;
  missing_policy?: number;
  below_safety_stock?: number;
  at_or_below_reorder_point?: number;
  missing_data_ranking?: { input: string; pairs: number }[];
  by_change?: Record<string, number>;
  classification_note?: string;
  note?: string;
}
export interface SummaryResponse {
  analysis: Analysis;
  summary: AnalysisSummary;
  warnings: { code: string; text: string }[];
  failures: unknown[];
  inputs: Record<string, unknown>;
  disclaimer: string;
}

export interface Finding {
  id: number;
  analysis_id: number;
  product_id: number;
  facility_id: number;
  product_code: string;
  product_name: string;
  facility_code: string;
  facility_name: string;
  unit: string | null;
  status: StockStatus;
  severity_rank: number;
  reason: string;
  change: string | null;
  confidence: Confidence;
  coverage: number;
  days_of_inventory: number | null;
  days_of_inventory_status: string | null;
  stockout_within_day: number | null;
  explanation: string;
}
export interface Projection {
  series: number[];
  receipts_by_day: Record<string, number>;
  min_closing: number;
  stockout_within_day: number | null;
  stockout_precision?: string;
  unfilled_demand: number;
  unfilled_demand_note?: string;
  formula: string;
  opening: number;
  rate_per_day: number;
  horizon_days: number;
  as_of: string;
  receipts_counted_detail?: { id: number; day: number; quantity: number; reference?: string }[];
  receipts_excluded?: { id: number; reason: string; reference?: string }[];
  crossings?: Record<string, number | null>;
  demand_basis?: string;
  requested_horizon_days?: number;
  observed_rate_per_day?: number | null;
  demand_source?: DemandSource;
}
export interface DemandSource {
  source: "forecast" | "observed_rate";
  label: string;
  forecast_run_id?: number;
  run_uid?: string;
  model?: string | null;
  origin?: string | null;
  validated?: boolean;
  unit?: string | null;
  covered_days?: number;
  note?: string;
  fallback_from_forecast?: { forecast_run_id: number; status: string; reason: string };
}
export interface ScenarioBlock {
  impact_analysis_id: number | null;
  facility_exposed: boolean;
  outcome: "NOT_EXPOSED" | "NOT_PROJECTABLE" | "EXPOSED_NO_MODELLED_SUPPLY" | "QUANTIFIED" | "CONDITIONAL_BOUNDS";
  note?: string;
  effects: Record<string, unknown>[];
  series?: number[];
  series_worst?: number[];
  series_best?: number[];
  stockout_within_day?: number | null;
  stockout_within_day_worst?: number | null;
  status?: StockStatus;
  status_worst?: StockStatus;
  status_best?: StockStatus;
}
export interface FindingDetail extends Finding {
  quantities: Record<string, unknown>;
  demand: Record<string, unknown>;
  coverage_days: Record<string, unknown>;
  policy: Record<string, unknown>;
  projection: Projection | null;
  scenario: ScenarioBlock | null;
  confidence_basis: string[];
  coverage_detail: Record<string, boolean>;
  data_quality: { code: string; severity: string; message: string }[];
  assumptions: string[];
  evidence: Record<string, unknown>;
  disclaimer: string;
}
export interface CompareItem {
  product_id: number;
  facility_id: number;
  product_name: string;
  facility_name: string;
  baseline_status: StockStatus | null;
  scenario_status: StockStatus | null;
  change: string | null;
  baseline_stockout_day: number | null;
  scenario_stockout_day: number | null;
  scenario_stockout_day_worst: number | null;
  scenario_outcome: string | null;
  scenario_finding_id: number | null;
  baseline_finding_id: number | null;
}
export interface Comparison {
  baseline_analysis_id: number;
  scenario_analysis_id: number;
  comparable: boolean;
  caveats: { code: string; text: string }[];
  pairs: number;
  changed: number;
  not_assessable: number;
  items: CompareItem[];
  total: number;
  limit: number;
  offset: number;
  impact_analysis_id: number | null;
  note: string;
}
export interface FindingFilters {
  limit: number;
  offset: number;
  status?: StockStatus | "";
  confidence?: Confidence | "";
  q?: string;
  change?: string;
}
