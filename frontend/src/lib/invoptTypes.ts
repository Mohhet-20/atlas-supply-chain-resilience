// Mirrors the /api/v1/replenishment contract. Cost figures may be null for viewers (cost_redacted). All example data in tests is SYNTHETIC.
export interface Page<T> { items: T[]; total: number; limit: number; offset: number }

export type PolicyType = "S_Q" | "S_UPTO" | "R_S";
export type ServiceType = "CYCLE" | "FILL_RATE";
export type RunStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "PARTIAL" | "FAILED" | "CANCELLED";
export type ApprovalState = "PROPOSED" | "APPROVED" | "REJECTED";
export interface Approval { state: ApprovalState; decided_by: string | null; decided_at: string | null; rationale: string | null; executed: boolean }

export const POLICY_TYPES: { value: PolicyType; label: string; hint: string }[] = [
  { value: "S_UPTO", label: "(s, S) continuous review", hint: "When the inventory position reaches s, order up to S." },
  { value: "S_Q", label: "(s, Q) continuous review", hint: "When the inventory position reaches s, order a fixed quantity Q." },
  { value: "R_S", label: "(R, S) periodic review", hint: "Every R days, order up to S." },
];
export const SERVICE_TYPES: { value: ServiceType; label: string; hint: string }[] = [
  { value: "CYCLE", label: "Cycle service level", hint: "Probability of no stockout in a replenishment cycle." },
  { value: "FILL_RATE", label: "Fill rate", hint: "Share of demand units served from stock. Needs an order quantity." },
];

export interface Issue { code: string; severity: string; message: string }

export interface Health {
  algorithm_version: string; write_api_enabled: boolean; cost_profiles: number; runs_by_status: Record<string, number>;
  limits: Record<string, number>; algorithms: string[]; not_supported: string[]; statements: Record<string, string>;
}

export interface PositionRow {
  product_id: number; facility_id: number; product_code: string | null; product_name: string | null; facility_code: string | null; facility_name: string | null;
  unit: string | null; available: number | null; usable: number | null; backordered: number | null; observed_at: string | null; age_days: number | null; stale: boolean;
  inventory_status: string; demand_per_day: number | null; demand_status: string | null; days_of_cover: number | null; has_policy: boolean; reorder_point: number | null;
  status: "AT_OR_BELOW_REORDER_POINT" | "ABOVE_REORDER_POINT" | "UNKNOWN"; flags: string[];
}
export interface Positions extends Page<PositionRow> { capped: boolean; considered: number; gaps_on_page: Record<string, number>; statement: string }

export interface CostProfile {
  profile_uid: string; product_id: number; facility_id: number; currency: string; holding_cost_per_unit_day: number | null; ordering_cost: number | null;
  shortage_cost_per_unit: number | null; unit_cost: number | null; storage_capacity: number | null; unit: string | null; provenance: string; source_note: string | null;
  effective_from: string; created_by: string; created_at: string; retired_at: string | null; cost_redacted?: boolean;
}

export interface Metric { mean: number | null; p05: number | null; p95: number | null }
export interface CostMetric { holding: Metric | null; ordering: Metric | null; shortage: Metric | null; known_subtotal: Metric | null; purchase_spend: Metric | null; omitted_components: string[] }
export interface SimMetrics {
  fill_rate: Metric; cycle_service_level: Metric; stockout_days: Metric; stockout_events: Metric; units_short: Metric; avg_on_hand: Metric; orders: Metric; turnover: Metric; costs: CostMetric;
}
export interface PolicyShape { type: PolicyType; reorder_point: number | null; order_quantity: number | null; order_up_to: number | null; review_period_days: number; min_order_quantity: number | null; order_increment: number | null; max_order_quantity: number | null }
export interface Candidate { id: string; ss_multiplier: number | null; q_multiplier: number | null; safety_stock: number | null; policy: PolicyShape; flags: string[]; meets_service_target: boolean; metrics: SimMetrics }
export interface Evaluation {
  mode: string; synthetic: boolean; statement: string; assumptions: string[]; candidates: Candidate[]; selected: string | null; selection_rule?: string; analytic_id: string;
  budget: { replications: number; days: number; candidates: number; max_sim_steps: number; note: string | null }; seed: number;
  recommended: { candidate: string; metrics: SimMetrics; sample_path: { on_hand: number[]; inventory_position: number[]; truncated: boolean } | null; meets_service_target: boolean } | null;
  current: { available: boolean; label?: string; reason?: string | null; policy?: PolicyShape; metrics?: SimMetrics } | null;
  comparison: { basis: string; fill_rate_change: number | null; cycle_service_level_change: number | null; units_short_change: number | null; avg_on_hand_change: number | null; orders_change: number | null; known_cost_change: number | null; known_cost_note: string | null; omitted_cost_components: string[] } | null;
  backtest: Backtest | null;
}
export interface Backtest {
  status: string; reason?: string; train_days: number; test_days: number; leakage_guard: string; limitations: string[]; note?: string; design?: string;
  results?: Record<string, { fill_rate: number | null; cycle_service_level: number | null; stockout_days: number; units_short: number; avg_on_hand: number; orders: number; completed_cycles: number }>;
}
export interface Recommendation {
  status: string; order_now: boolean; quantity: number; flags: string[]; inventory_position: number; trigger: boolean; rule: string | null; arrival_after_days: number | null; arrival_date: string | null;
  unit: string | null; purchase_spend: number | null; currency: string | null; supplier_id: number | null; decision_support_only: boolean; statement: string; policy: PolicyShape; safety_stock: number;
  service_target: { type: ServiceType; target: number }; shortfall_vs_target?: number;
}
export interface ScenarioRow {
  name: string; source: string; events: string[]; effective: { demand_per_day: number; lead_time_days: number; orders_blocked: [number, number] | null };
  undisrupted_recommended: SimMetrics; recommended_as_is: SimMetrics; current_policy: SimMetrics | null; replanned?: { policy?: PolicyShape; safety_stock?: number; metrics?: SimMetrics; status?: string; reason?: string };
  degradation: { fill_rate_change: number | null; cycle_service_level_change: number | null; units_short_change: number | null };
}
export interface Mitigation { scenario: string; option: string; description: string; extra_safety_stock?: number; quantity?: number; feasible_now?: boolean; note?: string; metrics: SimMetrics; improvement_vs_as_is: { fill_rate_change: number | null; units_short_change: number | null } }

export interface RunListItem {
  run_uid: string; kind: "OPTIMIZE" | "SIMULATE"; status: RunStatus; result_status: string | null; requested_by: string; product_id: number; facility_id: number; scenario_id: number | null;
  forecast_run_id: number | null; policy_type: PolicyType | null; service_type: ServiceType | null; service_target: number | null; objective: string | null; algorithm_version: string;
  snapshot_hash: string | null; scenario_hash: string | null; recommendation_hash: string | null; queued_at: string; started_at: string | null; finished_at: string | null; duration_ms: number | null;
  error: string | null; order_now: boolean | null; order_quantity: number | null; order_status: string | null; unit: string | null; safety_stock: number | null; hypothetical: boolean;
  approval: Approval | null; cost_redacted?: boolean;
}
export interface RunDetail extends Omit<RunListItem, "safety_stock"> {
  request: Record<string, unknown>; request_hash: string;
  position: { status: string; unit: string | null; available: number | null; usable: number | null; backordered: number | null; on_order_counted: number; inventory_position: number | null; stale: boolean | null; age_days: number | null; formula: string; receipts_counted: { id: number; day: number; quantity: number; status: string; reference: string | null }[]; receipts_excluded: { id: number; reason: string; quantity: number }[]; backordered_note: string | null } | null;
  demand: { mean: number | null; sd: number | null; source: string | null; sd_source: string | null; intermittent: boolean; observed_mean: number | null; unit: string | null; coverage_ratio: number | null; forecast: { forecast_run_id?: number; model?: string; model_version?: string; origin?: string; data_snapshot_hash?: string; label?: string } | null; statement: string } | null;
  lead_time: { days: number | null; std: number | null; source: string | null; provenance: string | null; n: number; alternatives: { source: string; days: number | null; std: number | null; n?: number; note?: string }[] } | null;
  safety_stock: { value: number | null; method: string | null; service_type: ServiceType; service_target: number; protection_days?: number; mean_protection_demand?: number; reorder_level?: number; reason?: string | null; current: number | null; calculated: { assumptions: string[]; warnings: string[]; formula?: string; implied?: { cycle_service_level: number | null; fill_rate: number | null } } | null } | null;
  recommendation: Recommendation | null;
  projection: { horizon_days: number; demand_basis: string; series: number[]; stockout_within_day: number | null; min_closing: number; unfilled_demand: number; days_until_reorder_point: number | null; stockout_risk_before_next_arrival: { value: number | null; basis: string | null }; note: string } | null;
  evaluation: Evaluation | null; scenario_results: ScenarioRow[] | null; mitigations: Mitigation[] | null;
  infeasibility: { code: string; message: string; details?: unknown }[] | null;
  costs: { currency: string | null; inputs: Record<string, number | null>; provenance: Record<string, string>; missing_inputs: string[]; statement: string; recommended_policy: (Omit<CostMetric, "holding" | "ordering" | "shortage" | "known_subtotal" | "purchase_spend"> & { holding: Metric | null; ordering: Metric | null; shortage: Metric | null; known_subtotal: Metric | null; purchase_spend: number | null; horizon: string }) | null; current_policy: unknown; order_purchase_spend: number | null } | null;
  explanation: { steps: { step: string; text: string }[]; sensitivity: { change: string; safety_stock: number; reorder_level: number; basis: string }[] | null; order_quantity: { method: string | null; reason: string | null } | null; constraints: Record<string, unknown> | null } | null;
  warnings: { warnings: Issue[]; data_quality: Issue[] } | null;
  provenance: { as_of: string; snapshot_hash: string; scenario: { scenario: { name: string; scenario_uid: string }; applied: { source: string; effect: string }[]; not_applied: { source: string; event_type: string; reason: string }[] } | null; hypothetical: boolean; lead_time_source: string | null; lead_time_provenance: string | null; demand_source: string | null } | null;
  reproducibility: Record<string, unknown>; statements: string[];
}
export interface DecisionHistory { run_uid: string; current: Approval; history: { decision: string; rationale: string; decided_by: string; decided_at: string; recommendation_hash: string }[]; statement: string }
export interface CalcSafety { result_status: string; safety_stock: RunDetail["safety_stock"]; demand: RunDetail["demand"]; lead_time: RunDetail["lead_time"]; order_quantity: { method: string | null; reason: string | null } | null; infeasibility: { code: string; message: string }[]; warnings: Issue[]; data_quality: Issue[]; sensitivity: { change: string; safety_stock: number; reorder_level: number }[] | null; snapshot_hash: string }
export interface CalcPolicy { result_status: string; recommendation: Recommendation | null; safety_stock: RunDetail["safety_stock"]; position: RunDetail["position"]; projection: RunDetail["projection"]; infeasibility: { code: string; message: string }[]; explanation: { step: string; text: string }[]; warnings: Issue[]; data_quality: Issue[]; label: string }
