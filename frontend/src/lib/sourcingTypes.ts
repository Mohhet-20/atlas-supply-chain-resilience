// Mirrors the /api/v1/sourcing contract. Fields that may be hidden from viewers (commercial figures) are nullable.
export interface Page<T> { items: T[]; total: number; limit: number; offset: number }

export type EligibilityStatus = "ELIGIBLE" | "ELIGIBLE_AFTER_QUALIFICATION" | "MISSING_INFORMATION" | "UNAVAILABLE" | "EXCLUDED" | "DISQUALIFIED";
export type Objective = "LOWEST_COST" | "LOWEST_LANDED_COST" | "RELIABILITY" | "LOWEST_RISK" | "BALANCED" | "RESILIENCE";
export type Strategy = "AUTO" | "SINGLE" | "DUAL" | "MULTI";
export type ResultStatus = "feasible" | "partial" | "infeasible" | "incomplete" | "not_solved";
export type RunStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";
export type ApprovalState = "PROPOSED" | "APPROVED" | "REJECTED";

export const OBJECTIVES: { value: Objective; label: string; hint: string }[] = [
  { value: "LOWEST_COST", label: "Lowest purchase cost", hint: "Minimises quantity × unit price. Freight is not included." },
  { value: "LOWEST_LANDED_COST", label: "Lowest landed cost", hint: "Includes freight only where every used offer states it; otherwise the run says so." },
  { value: "RELIABILITY", label: "Most reliable", hint: "Uses measured delivery reliability only where the sample is large enough." },
  { value: "LOWEST_RISK", label: "Lowest risk", hint: "Uses the supplier risk scores already stored; no score is invented." },
  { value: "BALANCED", label: "Balanced", hint: "Weighted suitability score, with the weights you set." },
  { value: "RESILIENCE", label: "Resilience", hint: "Minimises the share lost if the largest single supplier fails." },
];
export const STRATEGIES: Strategy[] = ["AUTO", "SINGLE", "DUAL", "MULTI"];
export const METRICS = ["price", "lead_time", "reliability", "quality", "capacity", "risk"] as const;

export interface Health {
  algorithm_version: string;
  write_api_enabled: boolean;
  offers: number;
  runs_by_status: Record<string, number>;
  limits: Record<string, number>;
  algorithms: string[];
  not_supported: string[];
  statements: Record<string, string>;
}

export interface Offer {
  offer_uid: string; supplier_id: number; product_id: number; destination_facility_id: number; approval_status: string; unit: string | null;
  unit_price: number | null; price_redacted?: boolean; currency: string | null; price_quality: string; freight_per_unit: number | null;
  capacity_amount: number | null; capacity_quality: string; min_order_quantity: number | null; lot_size: number | null; max_allocation: number | null;
  lead_time_days: number | null; lead_time_quality: string; defect_rate: number | null; contract_valid_from: string | null; contract_valid_to: string | null;
  status: string; provenance: string; observed_at: string | null; created_by: string; created_at: string;
}

export interface Evidence { rule: string; outcome: string; detail: string }
export interface ScoreComponent { metric: string; raw: number | null; normalised: number | null; weight: number; contribution: number; direction: string; available: boolean }
export interface Score { score: number; components: ScoreComponent[]; missing_metrics: string[]; complete: boolean; weight_coverage: number }
export interface Terms {
  unit: string | null; unit_price: number | null; currency: string | null; freight_per_unit: number | null; capacity: number | null; effective_capacity: number | null;
  min_order_quantity: number | null; lot_size: number | null; max_allocation: number | null; lead_time_days: number | null; defect_rate: number | null;
  approval: string; relationship: string; offer_status: string; observed_at: string | null; provenance: string; quality: Record<string, string>;
}
export interface Candidate {
  offer_uid: string | null; supplier_id: number; supplier_code: string; supplier_name: string; country: string | null; status: EligibilityStatus;
  reasons: string[]; missing: string[]; evidence: Evidence[]; rank: number | null; score: Score | null; objective_excluded: string | null;
  allocated_quantity: number; terms: Terms | null;
  risk: { score: number | null; source: string | null } | null;
  reliability: { value: number | null; n: number; interval: [number, number] | null; note: string } | null;
  transport: { status: string; detail?: string } | null; scenario_notes: string[];
}
export interface Alternatives {
  counts: Record<string, number>; price_basis: string; ranking_incomplete: boolean; candidates: Candidate[]; warnings: string[]; statements: string[]; cost_redacted?: boolean;
}

export interface Allocation {
  offer_uid: string; supplier_id: number; supplier_code: string; supplier_name: string; country: string | null; quantity: number; unit: string;
  unit_price: number | null; freight_per_unit: number | null; purchase_cost: number | null; lead_time_days: number | null; capacity: number | null;
  capacity_utilisation: number | null; suitability_score: number | null; rank: number | null; share: number;
}
export interface Metrics {
  currency: string | null; allocated_quantity: number; demand: number; unmet_quantity: number; fulfilment_pct: number; purchase_cost: number | null;
  freight_cost: number | null; landed_cost_stated: number | null; cost_basis: string; average_unit_cost: number | null; average_lead_time_days: number | null;
  slowest_lead_time_days: number | null; weighted_risk: number | null; supplier_count: number; hhi: number | null; effective_suppliers: number | null;
  max_supplier_share: number | null; country_hhi: number | null; country_shares: Record<string, number>; concentration_note: string;
}
export interface Approval { state: ApprovalState; decided_by: string | null; decided_at: string | null; rationale: string | null; executed: boolean }
export interface InfeasibilityReason { code: string; detail: string; [k: string]: unknown }
export interface Infeasibility { reasons: InfeasibilityReason[]; unmet_quantity?: number; information_that_would_help?: string[]; [k: string]: unknown }
export interface HardCheck { check: string; passed: boolean; detail?: string; [k: string]: unknown }

export interface RunListItem {
  run_uid: string; kind: "OPTIMIZE" | "COMPARE"; status: RunStatus; result_status: ResultStatus | null; requested_by: string; product_id: number;
  destination_facility_id: number; scenario_id: number | null; objective: Objective; strategy: Strategy; demand: number; unit: string; algorithm_version: string;
  snapshot_hash: string | null; queued_at: string; finished_at: string | null; duration_ms: number | null; error: string | null; supplier_count: number | null;
  unmet_quantity: number | null; hhi: number | null; hypothetical: boolean; approval: Approval | null; purchase_cost: number | null;
}
export interface Comparison {
  feasibility_changed: boolean; status: { baseline: string; scenario: string };
  purchase_cost: { baseline: number; scenario: number; change: number; change_pct: number | null } | null;
  hhi: { baseline: number; scenario: number; change: number } | null;
  unmet_quantity: { baseline: number; scenario: number; change: number } | null;
  average_lead_time_days: { baseline: number; scenario: number; change: number } | null;
  reallocation: { offer_uid: string; supplier_code: string; baseline_quantity: number; scenario_quantity: number; change: number }[];
  suppliers_lost: string[]; suppliers_gained: string[];
  changed_assumptions: { source: string; effect: string; offers_affected: number | null }[];
  not_applied: { event_type?: string; reason?: string; [k: string]: unknown }[]; note: string;
}
export interface StrategyRow {
  result_status: ResultStatus | null; supplier_count: number | null; purchase_cost: number | null; hhi: number | null;
  exposure_if_executed_unchanged?: { lost_share: number | null; [k: string]: unknown } | null;
  reoptimised_under_scenario?: { result_status: ResultStatus | null; [k: string]: unknown } | null;
  cost_of_diversification_vs_cheapest?: { amount: number | null; [k: string]: unknown } | null;
  [k: string]: unknown;
}
export interface RunDetail extends RunListItem {
  request: Record<string, unknown>; request_hash: string; candidates: Candidate[]; allocations: Allocation[]; baseline_allocations: Allocation[] | null;
  comparison: (Comparison & { strategies?: Record<string, StrategyRow> }) | Record<string, unknown> | null; infeasibility: Infeasibility | null;
  metrics: Metrics | null; disruptions: Record<string, unknown> | null; summary: { hard_constraint_checks?: HardCheck[]; solver?: { optimality: string; method: string; status: string }; price_basis?: string; ranking_incomplete?: boolean; [k: string]: unknown } | null;
  warnings: string[]; statements: string[]; reproducibility: Record<string, unknown> | null; cost_redacted?: boolean;
}
export interface DecisionHistory { run_uid: string; current: Approval; history: { decision: string; rationale: string; decided_by: string; decided_at: string }[]; statement: string }
export interface Validation { valid: boolean; problems: string[]; capacity_sufficient?: boolean; capacity_shortfall?: number | null }
