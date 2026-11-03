// Mirrors /api/v1/analytics. value null with state "no_data" means unknown; it is never zero.
export interface Page<T> { items: T[]; total: number; limit: number; offset: number }
export type MetricState = "ok" | "no_data" | "unavailable";
export interface Comparison { previous: number; change: number; change_pct: number | null; verdict: "better" | "worse" | "same" | "changed"; note: string | null }
export interface Metric {
  id: string; label: string; area: string; unit: string; value: number | string | null; state: MetricState; reason: string | null; as_of: string | null;
  direction: "min" | "max" | "neutral"; time_basis: string; comparison: Comparison | null; breakdown: Record<string, number | string> | null;
  filters_honoured: string[]; drilldown: string | null; warnings: string[];
}
export interface Meta {
  registry_version: string; generated_at: string; timezone: string; window: { days: number; start: string; end: string; previous_start: string };
  applied_filters: Record<string, string | number>; ignored_filters: string[]; cached: boolean; scope: string;
}
export interface Unavailable { id: string; label: string; reason: string }
export interface Finding { metric: string; severity: string; text: string; link: string; kind: "finding" }
export interface Overview { meta: Meta; metrics: Metric[]; findings: Finding[]; unavailable: Unavailable[]; findings_note: string }
export interface AreaView { meta: Meta; metrics: Metric[] }
export interface RiskRow {
  supplier_id: number; supplier_code: string; name: string; region: string; category: string; classification: string; risk_score: number | null; evidence_coverage: number | null;
  status: string | null; basis: string; evidence_latest_at: string | null; computed_at: string | null; requires_review: boolean; kind: string;
}
export interface Breakdown { key: string; total: number; LOW: number; MODERATE: number; HIGH: number; CRITICAL: number; UNKNOWN: number }
export interface RiskTrend { points: { date: string; suppliers_in_scope: number; classified: number; high_or_critical: number; unknown: number }[]; basis: string }
export interface SuppliersView {
  meta: Meta; metrics: Metric[]; table: Page<RiskRow> & { truncated: boolean }; trend: RiskTrend;
  breakdowns: { by_region: Breakdown[]; by_category: Breakdown[]; capacity_exposure: { unit: string; all_confirmed_capacity: number; high_or_critical_capacity: number; share: number | null }[]; capacity_note: string };
}
export interface InventoryRow { product_code: string; product_name: string; facility_code: string; facility_name: string; unit: string | null; status: string; reason: string; confidence: string; input_coverage: number }
export interface InventoryView {
  meta: Meta; metrics: Metric[];
  table: Page<InventoryRow> & { analysis: { analysis_uid: string; as_of: string | null; finished_at: string | null; algorithm_version: string; truncated: boolean } | null; by_status: Record<string, number>; note?: string };
}
export interface LogisticsView { meta: Meta; metrics: Metric[]; lead_time_distribution: { basis: string; n: number; bins: { label: string; count: number }[] }; unavailable: Unavailable[] }
export interface NetworkView {
  meta: Meta; metrics: Metric[]; criticality: Record<string, number>; note: string;
  single_source: Page<{ facility_code: string | null; facility_name: string | null; supplier_code: string | null; supplier_name: string | null; criticality: string | null; supply_share_pct: number | null }> & { note: string };
}
export interface RunRow { kind: string; uid: string; title: string; status: string | null; result_status: string | null; at: string | null; actor: string | null; link: string | null }
export interface DisruptionView { meta: Meta; scenarios: Metric[]; optimization: Metric[]; operations: Metric[]; runs: RunRow[]; note: string }
export interface AlertRow { source: string; id: number; severity: string; status: string; reason: string; supplier_id: number | null; at: string | null; link: string; occurrences: number }
export interface AlertsView extends Page<AlertRow> { meta: Meta; priority: string }
export interface ActivityView extends Page<RunRow> { audit_included: boolean; note: string }
export interface FilterValues { windows: number[]; regions: string[]; categories: string[]; risk_classes: string[]; unsupported: string[] }
export interface MetricDef { id: string; label: string; area: string; unit: string; calculation: string; time_basis: string; direction: string; sources: string[]; missing: string; filters: string[]; drilldown: string | null }
export interface MetricsRegistry { registry_version: string; metrics: MetricDef[]; unavailable: Unavailable[] }
export interface Health { registry_version: string; windows: number[]; default_window: number; export_areas: string[]; cache_ttl_seconds: number; export_max_rows: number; unavailable: Unavailable[]; scope: string }
export type Params = Record<string, string>;
