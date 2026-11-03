// Mirrors the /api/v1/resilience contract. Cost figures may be null (hidden from viewers) or unknown; null is never zero.
export interface Page<T> { items: T[]; total: number; limit: number; offset: number }

export type ObjectiveId = "COST" | "UNMET_DEMAND" | "DELIVERY_DELAY" | "CONCENTRATION" | "DISRUPTION_EXPOSURE" | "WORST_CASE_SERVICE";
export type StrategyId = "WEIGHTED_SUM" | "PARETO" | "EPSILON_CONSTRAINT";
export type StressKind = "SUPPLIER_OUTAGE" | "CAPACITY_DROP" | "LEAD_TIME_INCREASE" | "DEMAND_SURGE" | "SCENARIO";
export type ResultStatus = "completed" | "completed_with_warnings" | "infeasible" | "timed_out" | "invalid_input" | "insufficient_data" | "failed";
export type RunStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "PARTIAL" | "FAILED" | "CANCELLED";
export type ApprovalState = "PROPOSED" | "APPROVED" | "REJECTED";

export const STRESS_KINDS: { value: StressKind; label: string; param: string | null }[] = [
  { value: "SUPPLIER_OUTAGE", label: "Supplier outage", param: "supplier_id" },
  { value: "CAPACITY_DROP", label: "Capacity drop (multiplier below 1)", param: "capacity_multiplier" },
  { value: "LEAD_TIME_INCREASE", label: "Lead-time increase (days)", param: "add_lead_days" },
  { value: "DEMAND_SURGE", label: "Demand surge (multiplier above 1)", param: "demand_multiplier" },
  { value: "SCENARIO", label: "Registered scenario (id)", param: "scenario_id" },
];

export interface Approval { state: ApprovalState; decided_by: string | null; decided_at: string | null; rationale: string | null; executed: boolean; alternative_id?: string | null }

export interface CatalogueObjective {
  id: ObjectiveId; name: string; direction: "min" | "max"; unit: string; definition: string; inputs: string[]; normalisation: string; missing: string; explanation: string;
  availability?: { available: boolean; reason: string | null }; enabled?: boolean; weight?: number | null;
}
export interface Catalogue {
  objectives: CatalogueObjective[];
  strategies: { id: StrategyId; label: string; note: string }[];
  stress_kinds: { id: StressKind; label: string }[];
  scenario_modes: { id: "FIXED" | "ADAPTIVE"; note: string }[];
  constraints: { id: string; kind: "hard" | "soft"; description: string }[];
  result_statuses: ResultStatus[];
  statements: Record<string, string>;
}
export interface Health {
  algorithm_version: string; write_api_enabled: boolean; runs_by_status: Record<string, number>; limits: Record<string, number>;
  solver: { name: string; version: string; type: string; deterministic: boolean }; algorithms: string[]; not_supported: string[]; statements: Record<string, string>;
}
export interface Availability {
  objectives: { id: ObjectiveId; name: string; direction: string; unit: string; available: boolean; reason: string | null }[];
  eligibility_counts: Record<string, number>; eligible_capacity_total: number | null; demand: number;
  opening_stock: { value: number | null; source: string | null }; scenarios_applied: string[]; warnings: Warning[]; snapshot_hash: string; cost_redacted?: boolean;
}
export interface Warning { code: string; severity: string; message: string }

export interface Allocation { offer_uid: string; supplier_id: number; supplier_code: string; supplier_name: string; country: string | null; quantity: number; share: number; unit_price: number | null; lead_time_days: number | null }
export interface CostBlock { total_known: number | null; purchase: number | null; freight: number | null; shortage_penalty: number | null; omitted_components: string[]; basis: string; currency: string | null }
export interface ScenarioOutcome { name: string; kind: string; probability: number | null; mode: string; demand: number; delivered: number; supply_lost: number; from_stock: number; recovered: number; unmet: number; fulfilment: number }
export interface ConstraintCheck { id: string; passed: boolean; detail: string; slack: number | null; binding: boolean }
export interface Alternative {
  id: string; allocation: Allocation[]; total_quantity: number; values: Record<ObjectiveId, number | null>; cost: CostBlock; baseline_fulfilment: number | null;
  scenarios: ScenarioOutcome[]; expected_unmet: number | null; pareto: boolean; score: number | null; normalised: (number | null)[]; recommended: boolean; feasible: boolean;
  equivalent_plans: string[]; anchor: string | null; constraint_checks: ConstraintCheck[];
}
export interface ComparisonRow { objective: ObjectiveId; name: string; unit: string; direction: "min" | "max"; baseline: number | null; candidate: number | null; difference: number | null; relative_pct: number | null; verdict: "better" | "worse" | "same" | "unknown"; caveat: string | null }
export interface Comparison { reference: "SUPPLIED_BASELINE" | "REFERENCE_COST_MINIMAL"; reference_plan: Alternative; candidate_id: string; rows: ComparisonRow[]; note?: string }
export interface SensitivityVariant { parameter: string; label: string; result_status: ResultStatus; feasible: boolean; same_as_base: boolean; recommended_id: string | null; allocation: Allocation[]; values: Record<string, number | null> }
export interface Sensitivity { base_recommended: string | null; variants: SensitivityVariant[]; variants_skipped_by_limit: number; stopped_by_time_budget?: boolean; stable?: boolean }
export interface Pareto {
  front_size: number; shown: number; ids: string[]; objectives: ObjectiveId[]; ranges: { objective: ObjectiveId; ideal: number; nadir: number }[]; non_discriminating: ObjectiveId[];
  conflicts: { objective: ObjectiveId; best_plan: string; best_value: number; values_of_other_objectives_in_that_plan: Record<string, number | null> }[]; approximation: string | null;
}
export interface Scenarios {
  mode: string; response_days: number | null; label: string; weighted: boolean; results_for: string | null; expected_unmet: number | null; worst: string | null;
  definitions: { name: string; kind: string; probability: number | null; affected_offers: string[]; not_applied: { reason?: string }[] }[]; outcomes: ScenarioOutcome[]; baseline_results: ScenarioOutcome[];
}
export interface Diagnostics { eligible_capacity_total?: number; note?: string; best_achievable?: { constraint: string; best_value: number | null; required: number | null; diagnostic_only: boolean }[]; reasons?: { code: string; detail: string }[]; [k: string]: unknown }
export interface Recommendation { id: string; basis: string; allocation: Allocation[]; values: Record<ObjectiveId, number | null>; decision_support_only: boolean; statement: string }

export interface RunListItem {
  run_uid: string; status: RunStatus; result_status: ResultStatus | null; requested_by: string; product_id: number; destination_facility_id: number; strategy: StrategyId; scenario_mode: string;
  quantity: number | null; unit: string | null; currency: string | null; objectives_requested: ObjectiveId[]; stress_count: number; algorithm_version: string; snapshot_hash: string | null;
  recommendation_hash: string | null; queued_at: string; started_at: string | null; finished_at: string | null; duration_ms: number | null; error: string | null; recommended_id: string | null;
  alternative_count: number; front_size: number | null; inexact: boolean | null; approval: Approval; cost_redacted?: boolean;
}
export interface RunDetail extends RunListItem {
  request: Record<string, unknown>; request_hash: string;
  objectives: { registry: CatalogueObjective[]; unavailable: { id: ObjectiveId; reason: string }[]; enabled: ObjectiveId[]; strategy: StrategyId; weights: Record<string, number>; normalisation: { method?: string; ranges?: { objective: string; ideal: number; nadir: number }[] } & Record<string, unknown>; epsilon?: unknown };
  constraints: { hard: { id: string; description: string }[]; soft?: { id: string; description: string }[]; configured: Record<string, unknown>; recommended_checks: ConstraintCheck[] };
  scenarios: Scenarios | null; alternatives: Alternative[]; pareto: Pareto | null; recommendation: Recommendation | null; comparison: Comparison | null; sensitivity: Sensitivity | null; diagnostics: Diagnostics | null;
  explanation: string[]; warnings: Warning[]; solver: { name: string; version: string; type: string; deterministic: boolean };
  statistics: Record<string, unknown> & { generated?: number; feasible?: number; truncated?: boolean; elapsed_ms?: number }; provenance: Record<string, unknown>; reproducibility: { deterministic?: boolean } & Record<string, unknown>; statements: Record<string, string>;
}
export interface DecisionHistory { run_uid: string; current: Approval; history: { decision: string; alternative_id: string | null; rationale: string; decided_by: string; decided_at: string; recommendation_hash: string | null }[]; statement: string }
