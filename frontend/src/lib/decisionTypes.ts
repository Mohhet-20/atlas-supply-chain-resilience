// Mirrors the /api/v1/decision contract. A hidden value is null with `hidden: true`; null is never zero.
export interface Page<T> { items: T[]; total: number; limit: number; offset: number }
export type CaseStatus = "DRAFT" | "READY" | "EVALUATED" | "UNDER_REVIEW" | "APPROVED" | "REJECTED" | "SUPERSEDED";
export type EvidenceKind = "RESILIENCE_RUN" | "SOURCING_RUN" | "SIMULATION_RUN";
export type EvidenceRole = "BASELINE" | "ALTERNATIVE" | "SCENARIO_EXPOSURE";
export type Verdict = "better" | "worse" | "same" | "changed" | "unknown";
export type RowStatus = "comparable" | "incomparable" | "missing_baseline" | "missing_value";
export type ResultStatus = "completed" | "completed_with_warnings" | "insufficient_data" | "invalid_input";

export interface EvidenceRef { kind: EvidenceKind; run_uid: string; role: EvidenceRole; plan?: string | null; label?: string | null }
export interface Definition {
  title: string; purpose: string; description?: string | null; horizon_days?: number | null; scenario_ids: number[]; evidence: EvidenceRef[];
  probabilities: { condition: string; probability: number }[]; probability_basis?: string | null; assumptions: string[];
}
export interface Approval { state: string; executed: boolean; decided_by?: string | null; decided_at?: string | null; rationale?: string | null; alternative_key?: string | null; [k: string]: unknown }
export interface CaseItem {
  case_uid: string; title: string; status: CaseStatus; version: number; created_by: string; reviewer: string | null; cloned_from: string | null; superseded_by: string | null;
  current_evaluation_uid: string | null; selected_alternative: string | null; created_at: string | null; updated_at: string | null; allowed_transitions: CaseStatus[];
  current_result_status?: ResultStatus | null; alternative_count?: number | null;
}
export interface CaseDetail extends CaseItem { definition: Definition; approval: Approval; evaluation_count: number; event_count: number }
export interface MetricDef {
  id: string; name: string; category: string; unit: string; direction: "min" | "max" | "neutral"; time_basis: string; definition: string; missing: string;
  cost_sensitive: boolean; scope_dependent: boolean; [k: string]: unknown;
}
export interface Unavailable { id: string; name: string; reason: string }
export interface Metrics { registry_version: string; metrics: MetricDef[]; unavailable: Unavailable[] }
export interface Health {
  algorithm_version: string; metric_registry_version: string; write_api_enabled: boolean; cases_by_status: Record<string, number>; limits: Record<string, number>;
  statuses: CaseStatus[]; transitions: Record<string, CaseStatus[]>; evidence_kinds: EvidenceKind[]; not_supported: string[]; statements: Record<string, string>;
}
export interface Candidate {
  kind: EvidenceKind; run_uid: string; result_status: string; product_id?: number; destination_facility_id?: number; quantity?: number | null; unit?: string | null;
  recommended_id?: string | null; alternatives?: string[]; has_reference_plan?: boolean; scenario_id?: number | null; name?: string | null; entities_affected?: number | null; finished_at: string | null;
}
export interface ValidationReport {
  ok: boolean; issues: { code: string; text: string }[];
  evidence: { kind: string; run_uid: string; role: string; plan: string | null; ok: boolean; plans?: string[]; conditions?: string[]; problem?: string }[];
}
export interface Row {
  metric: string; condition: string; reference_condition: string | null; baseline: number | null; value: number | null; unit: string | null; direction: string;
  difference: number | null; relative_pct: number | null; relative_note: string | null; verdict: Verdict; status: RowStatus; reason: string | null; hidden?: boolean;
}
export interface CondComparison { condition: string; reference_condition: string | null; reference_note: string | null; rows: Row[]; counts: Counts; classification: string }
export interface Counts { better: number; worse: number; same: number; changed: number; comparable: number; incomparable: number; missing: number }
export interface PlanComparison { plan_key: string; label: string; role: string; scope_differences: string[]; conditions: CondComparison[]; counts: Counts; classification: string }
export interface AggMetric {
  metric: string; unit: string | null; n_conditions: number; of_conditions: number; min: { value: number; condition: string } | null; max: { value: number; condition: string } | null;
  worst: { value: number; condition: string } | null; best: { value: number; condition: string } | null; expected: { value: number; basis: string } | null; expected_reason: string | null; hidden?: boolean;
}
export interface Aggregation { plan_key: string; weighted: boolean; weighting_reason: string | null; probability_basis: string | null; metrics: AggMetric[] }
export interface Alternative {
  key: string; label: string; source: string; classification: string; improves: string[]; worsens: string[]; scenarios_improved: string[]; scenarios_worsened: string[];
  constraints: { all_passed: boolean; failed: string[]; checked: number }; feasible: boolean | null; prerequisites: string[]; uncertainties: string[]; suitability: string; cost_notes: string[];
}
export interface ImpactRow { entity?: string; name?: string; kind?: string; type?: string; depth?: number | null; path?: string[]; [k: string]: unknown }
export interface ExposureBlock { source?: Record<string, unknown>; summary?: Record<string, unknown>; rows?: ImpactRow[]; truncated?: boolean; total_rows?: number | null; [k: string]: unknown }
export interface EvalResult {
  result_status: ResultStatus; result_hash: string; algorithm_version: string; metric_registry_version: string; baseline: { key: string; label: string; scope: Record<string, unknown>; reference_kind: string | null } | null;
  conditions: { key: string; name: string; mode: string; probability: number | null }[]; comparisons: PlanComparison[]; aggregation: Aggregation[]; alternatives: Alternative[];
  exposure: ExposureBlock[]; warnings: { code: string; text: string }[]; issues: { code: string; text: string }[]; unavailable_metrics: Unavailable[]; statements: Record<string, string>; cost_redacted?: boolean;
}
export interface EvalSummary {
  evaluation_uid: string; case_version: number; result_status: ResultStatus; algorithm_version: string; metric_registry_version: string; result_hash: string; snapshot_hash: string;
  evaluated_by: string; evaluated_at: string | null; duration_ms: number | null; alternatives: { key: string; classification: string }[]; warning_count: number;
}
export interface Evaluation extends EvalSummary { result: EvalResult; cost_redacted?: boolean }
export interface EvalOutcome { case: CaseDetail; evaluation: Evaluation; reused: boolean }
export interface CaseEvent { id: number; kind: string; from_status: string | null; to_status: string | null; actor: string; at: string | null; text: string | null; evaluation_uid: string | null; alternative_key: string | null; data: Record<string, unknown> | null }
export interface VersionItem { version: number; definition_hash: string; created_by: string; created_at: string | null; definition: Definition }
export interface Replay { evaluation_uid: string; reproduced_from_stored_snapshot: boolean; stored_result_hash: string; replayed_result_hash: string; live_evidence_unchanged: boolean; live_note?: string; explanation: string }
