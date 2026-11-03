/** Types for the Demand Forecasting API. A forecast is a model estimate, never an observation. */
import type { SimStatus } from "./scenarioTypes";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export type Frequency = "daily" | "weekly";
export type PatternLabel = "stable" | "trending" | "seasonal" | "volatile" | "intermittent" | "sparse" | "insufficient_history" | "poor_data_quality" | "unclassified";

export interface ForecastConfig {
  frequency: Frequency;
  horizon: number;
  lookback_periods: number;
  origin: string | null;
  basis: string;
  unit: string | null;
  seasonal_period: number | null;
  candidates: string[] | null;
  n_folds: number;
  interval_levels: number[];
  missing_policy: string;
  outlier_handling: string;
  selection_metric: string;
  nonnegative: boolean;
  [k: string]: unknown;
}

export interface ForecastLimits {
  algorithm_version: string;
  max_horizon_daily: number;
  max_horizon_weekly: number;
  max_history_periods: number;
  max_folds: number;
  max_candidates: number;
  max_active_runs: number;
  time_budget_seconds: number;
  finalization_days: number;
  defaults: ForecastConfig;
  models: string[];
  metrics: Record<string, [string, string]>;
  statements: Record<string, string>;
  write_enabled: boolean;
  organisation_model: string;
}

export interface ModelInfo {
  key: string;
  label: string;
  family: string;
  complexity_rank: number;
  version: string;
  frequencies: string[];
  needs_seasonal_period: boolean;
  intermittent_only: boolean;
  interval_support: string;
}

export interface RunSummary {
  id: number;
  run_uid: string;
  status: SimStatus;
  result_status: string | null;
  requested_by: string;
  product_id: number;
  facility_id: number;
  frequency: Frequency;
  horizon: number;
  unit: string | null;
  origin: string | null;
  selected_model: string | null;
  model_version: string | null;
  algorithm_version: string;
  config_hash: string;
  data_snapshot_hash: string | null;
  provenance: string;
  queued_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  error: string | null;
  fallback: { used?: string; reason?: string; validated?: boolean } | null;
  selection_status: string | null;
  patterns: PatternLabel[] | null;
  disclaimer: string;
}

export interface Selection {
  status: string;
  selected: string | null;
  metric: string;
  rationale: string;
  ranking: { model: string; score: number }[];
  best_baseline?: string | null;
}

export interface MetricValue {
  value: number | null;
  n: number;
  note: string | null;
}
export type MetricSet = Record<"mae" | "rmse" | "wape" | "mase" | "bias" | "smape", MetricValue>;
export interface ModelEvaluation {
  overall: MetricSet;
  per_fold: Record<string, MetricSet>;
  per_horizon: Record<string, MetricSet>;
  complete: boolean;
  failed_folds: number;
  seconds?: number;
}

export interface RunDetail extends RunSummary {
  config: ForecastConfig;
  model_params: Record<string, unknown> | null;
  training_interval: { start: string; end_exclusive: string } | null;
  preparation: { status: string; counts: Record<string, number>; imputations: unknown[]; outliers: unknown[]; warnings: string[]; steps: { step: string }[] } | null;
  diagnostics: Record<string, unknown> | null;
  patterns_detail: { labels: PatternLabel[]; evidence: Record<string, unknown>; criteria: Record<string, string> } | null;
  candidates: { evaluated: string[]; ineligible: Record<string, string> } | null;
  selection: Selection | null;
  validation: { scheme: string; fold_train_lengths: number[]; evaluation_horizon: number; configured_horizon: number; status: string; note: string | null; leakage_controls: string[] } | null;
  clamping: { enabled: boolean; points_clamped: number; bounds_clamped: number; total_point_shift: number } | null;
  reproducibility: Record<string, unknown> | null;
  timings: Record<string, number> | null;
  warnings: string[];
  statements: Record<string, string>;
}

export interface IntervalBound {
  level: number;
  lower: number | null;
  upper: number | null;
  status: "supported" | "unsupported";
  n?: number;
}
export interface ForecastValue {
  step: number;
  period_start: string;
  period_end: string;
  point: number;
  point_raw: number;
  clamped_at_zero: boolean;
  intervals: IntervalBound[];
  unit: string | null;
  model: string | null;
  model_version: string | null;
  origin: string | null;
  kind: "forecast";
}
export interface ValuesResponse {
  run: RunSummary;
  items: ForecastValue[];
  total: number;
  disclaimer: string;
  note: string | null;
}
export interface IntervalsResponse {
  calibration: { level: number; supported: boolean; reason?: string; method?: string; coverage_evaluation?: { status: string; n_scored: number; empirical_coverage: number | null; mean_width: number | null; note: string } }[];
  statement: string;
  assumptions: string[];
}

export interface SeriesItem {
  product_id: number;
  facility_id: number;
  product_code: string;
  product_name: string;
  facility_code: string;
  facility_name: string;
  observations: number;
  first_period: string | null;
  last_period: string | null;
  kinds: string[];
  units: string[];
  provenance: string[];
  latest_run: RunSummary | null;
}

export interface SeriesPoint {
  period_start: string;
  raw: number | null;
  value: number | null;
  observed: boolean;
  imputed: boolean;
}
export interface Diagnostics {
  preparation: { status: string; counts: Record<string, number>; warnings: string[]; unit: string | null; basis: string | null };
  points?: SeriesPoint[];
  diagnostics: {
    n: number;
    mean: number | null;
    median: number | null;
    std: number | null;
    cv: number | null;
    cv_note: string | null;
    zero_proportion: number;
    missing_proportion: number;
    coverage: number;
    trend: { relative_drift: number | null; t_stat: number | null; note: string | null };
    seasonality: { period: number | null; acf: number | null; note?: string };
    recent_vs_historical: { relative_change: number | null; note?: string };
    outliers: { count: number };
  } | null;
  patterns: { labels: PatternLabel[]; evidence: Record<string, unknown>; criteria: Record<string, string> };
  statements: Record<string, string>;
}

export interface ConfigValidation {
  valid: boolean;
  errors: { field: string; message: string }[];
  config: ForecastConfig | null;
}

export interface PreviewResponse {
  preparation_status: string;
  eligible: string[];
  ineligible: Record<string, string>;
  validation_plan: { folds: number; evaluation_horizon: number; min_train_periods: number; status: string; note: string | null } | null;
}

export interface ActualMatch {
  step: number;
  period_start: string;
  period_end: string;
  forecast: number;
  actual: number | null;
  status: "final" | "preliminary" | "partial" | "missing";
  intervals: IntervalBound[];
}
export interface ForecastVsActual {
  matches: ActualMatch[];
  counts: Record<string, number>;
  metrics: { final: MetricSet; interval_coverage: { level: number; n: number; empirical_coverage: number | null }[] } | null;
  note: string;
}

export interface Alert {
  id: number;
  kind: string;
  severity: string;
  product_id: number;
  facility_id: number;
  run_id: number;
  message: string;
  evidence: Record<string, unknown>;
  created_at: string;
}
