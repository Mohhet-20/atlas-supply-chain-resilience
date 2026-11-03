/** Types mirroring the Supplier Risk API (backend/app/api/v1/risk.py, app/schemas/supplier_risk.py). */

export type RiskClass = "LOW" | "MODERATE" | "HIGH" | "CRITICAL";
export type AssessmentStatus = "ASSESSED" | "PARTIAL" | "INSUFFICIENT_EVIDENCE" | "NOT_ASSESSED";
export type FactorState = "SCORED" | "UNAVAILABLE" | "NOT_APPLICABLE" | "INSUFFICIENT_EVIDENCE";
export type ReliabilityBand = "HIGH" | "MODERATE" | "LOW";
export type ReviewAction = "ACKNOWLEDGE" | "NOTE" | "OVERRIDE" | "REVOKE_OVERRIDE" | "REQUEST_EVIDENCE" | "FOLLOW_UP";
export type AlertStatus = "OPEN" | "ACKNOWLEDGED" | "DISMISSED" | "RESOLVED";
export type AlertType =
  | "SCORE_INCREASE"
  | "NEW_SINGLE_SOURCE"
  | "EVIDENCE_STALE"
  | "RELATIONSHIP_UNVERIFIED"
  | "ASSESSMENT_UNAVAILABLE"
  | "METHODOLOGY_CHANGE";
export type RunScope = "SUPPLIER" | "SUPPLIER_SET" | "NETWORK";

export const CLASSES: RiskClass[] = ["LOW", "MODERATE", "HIGH", "CRITICAL"];
export const ASSESSMENT_STATUSES: AssessmentStatus[] = ["ASSESSED", "PARTIAL", "INSUFFICIENT_EVIDENCE", "NOT_ASSESSED"];
export const ALERT_TYPES: AlertType[] = [
  "SCORE_INCREASE",
  "NEW_SINGLE_SOURCE",
  "EVIDENCE_STALE",
  "RELATIONSHIP_UNVERIFIED",
  "ASSESSMENT_UNAVAILABLE",
  "METHODOLOGY_CHANGE",
];

export interface Interpretation {
  meaning: string;
  not_a_probability: boolean;
  not_a_prediction: string;
  missing_data: string;
}

export interface Evidence {
  evidence_type: string;
  ref: string;
  summary: string;
  observed_at: string | null;
  provenance: string | null;
  source_namespace: string | null;
  reasoning: string;
}

export interface Factor {
  id: number;
  category: string;
  label: string;
  role: string;
  state: FactorState;
  basis: string;
  score: number | null;
  severity: string | null;
  weight: number;
  explanation: string;
  unavailable_reason: string | null;
  missing_inputs: string[];
  evidence_observed_at: string | null;
  freshness: string;
  confidence: number | null;
  quality_context: Record<string, unknown>;
  requires_review: boolean;
  details: Record<string, unknown>;
  method_version: string;
  data_available: boolean;
  evidence: Evidence[] | null;
}

export interface Contribution {
  category: string;
  score: number | null;
  weight: number;
  group: string | null;
  counted: boolean;
  points: number | null;
  note: string | null;
}

export interface MissingFactor {
  category: string;
  role: string;
  reason: string;
  kind: string;
  missing_inputs: string[];
  data_dependency: string | null;
  weight: number;
}

export interface Assessment {
  id: number;
  assessment_uid: string;
  supplier_id: number;
  run_id: number;
  computed_at: string;
  status: AssessmentStatus;
  methodology_version: string;
  methodology_hash: string;
  risk_score: number | null;
  classification: RiskClass | null;
  evidence_coverage: number;
  effective_weight: number;
  reliability: number | null;
  reliability_band: ReliabilityBand | null;
  scored_factors: number;
  contributions: Contribution[];
  missing: MissingFactor[];
  actions: string[];
  data_basis: {
    contains_synthetic_or_simulated?: boolean;
    all_evidence_synthetic?: boolean;
    unavailable_categories?: string[];
    max_possible_coverage?: number;
    [k: string]: unknown;
  };
  evidence_latest_at: string | null;
  evidence_earliest_at: string | null;
  requires_review: boolean;
  review_state: "UNREVIEWED" | "ACKNOWLEDGED" | "OVERRIDDEN";
  is_current: boolean;
  superseded_at: string | null;
  supersedes_id: number | null;
  last_confirmed_at: string;
  rank_comparable: boolean;
  interpretation: Interpretation;
}

export interface AssessmentDetail extends Assessment {
  factors: Factor[];
}

export interface HistoryItem extends Assessment {
  score_change: number | null;
  change_comparable: boolean | null;
}

export interface Review {
  id: number;
  supplier_id: number;
  assessment_id: number;
  action: ReviewAction;
  factor_category: string | null;
  actor: string;
  actor_role: string;
  created_at: string;
  note: string | null;
  override_classification: RiskClass | null;
  override_expires_at: string | null;
  target_review_id: number | null;
  original_status: string;
  original_classification: RiskClass | null;
  original_score: number | null;
}

export interface Override extends Review {
  expired: boolean;
  stale: boolean;
  effective: boolean;
  note_on_staleness: string | null;
}

export interface Alert {
  id: number;
  supplier_id: number;
  supplier_name: string | null;
  assessment_id: number | null;
  alert_type: AlertType;
  severity: string;
  status: AlertStatus;
  reason: string;
  evidence: Record<string, unknown>;
  run_id: number | null;
  created_at: string;
  updated_at: string;
  last_seen_at: string;
  occurrences: number;
  resolved_at: string | null;
  resolved_by: string | null;
  resolution_note: string | null;
  version: number;
  delivery: string;
}

export interface SupplierBrief {
  id: number;
  supplier_code: string;
  name: string;
  country_or_region: string;
  category: string;
  status: string;
  provenance: string;
}

export interface SupplierRisk {
  supplier: SupplierBrief;
  assessed: boolean;
  assessment: AssessmentDetail | null;
  override: Override | null;
  reviews: Review[];
  alerts: Alert[];
  unassessed_note: string | null;
}

export interface SupplierRow {
  supplier_id: number;
  supplier_code: string;
  name: string;
  country_or_region: string;
  category: string;
  supplier_status: string;
  provenance: string;
  assessment_status: AssessmentStatus;
  risk_score: number | null;
  classification: RiskClass | null;
  evidence_coverage: number | null;
  reliability: number | null;
  reliability_band: ReliabilityBand | null;
  assessment_id: number | null;
  computed_at: string | null;
  last_confirmed_at: string | null;
  review_state: string | null;
  requires_review: boolean;
  methodology_version: string | null;
  rank_comparable: boolean;
  top_factors: Contribution[];
  contains_synthetic: boolean;
  override: { classification: RiskClass; effective: boolean } | null;
}

export interface SupplierPage {
  items: SupplierRow[];
  total: number;
  limit: number;
  offset: number;
  sort: string;
  ordering_note: string;
}

export interface SupplierFilters {
  q?: string;
  classification?: RiskClass | "";
  assessment_status?: AssessmentStatus | "";
  review_state?: string;
  requires_review?: boolean;
  region?: string;
  sort?: string;
  limit?: number;
  offset?: number;
}

export interface UnavailableCategory {
  category: string;
  label: string;
  data_dependency: string | null;
}

export interface Run {
  id: number;
  run_uid: string;
  scope_type: RunScope;
  scope_ref: Record<string, unknown>;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "PARTIAL" | "FAILED" | "CANCELLED";
  trigger: string;
  reason: string | null;
  requested_by: string;
  idempotency_key: string | null;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  methodology_version: string | null;
  methodology_hash: string | null;
  graph_snapshot: Record<string, unknown>;
  examined: number;
  completed_assessments: number;
  assessed: number;
  partial: number;
  insufficient_evidence: number;
  created: number;
  unchanged: number;
  failed: number;
  skipped: number;
  alerts_created: number;
  alerts_updated: number;
  failures: { supplier_id?: number; error?: string }[];
  truncated: boolean;
  truncation_reasons: string[];
  error: string | null;
}

export interface RunPage {
  items: Run[];
  total: number;
  limit: number;
  offset: number;
}

export interface Overview {
  generated_at: string;
  methodology_version: string;
  counts_note: string;
  suppliers_in_scope: number;
  assessed_suppliers: number;
  not_assessed: number;
  by_status: Record<string, number>;
  by_classification: Record<RiskClass, number>;
  classified: number;
  insufficient_evidence: number;
  requires_review_unreviewed: number;
  high_dependency_exposure: number;
  with_single_source_dependency: number;
  aging_assessments: number;
  assessment_stale_days: number;
  alerts_open_by_type: Record<string, number>;
  mixed_methodology: boolean;
  methodology_versions_in_use: Record<string, number>;
  mean_evidence_coverage: number | null;
  max_possible_coverage: number;
  run_health: { recent_runs: number; last_run: Run | null; last_run_incomplete: boolean };
  unavailable_categories: UnavailableCategory[];
}

export interface CategoryDoc {
  category: string;
  label: string;
  role: string;
  weight: number;
  data_available: boolean;
  data_dependency: string | null;
  required_inputs: string[];
  method: string;
  evidence_requirements: string;
  freshness_policy: string;
  uncertainty_treatment: string;
  unavailable_when: string;
  limitations: string[];
  correlation_group: string | null;
}

export interface Methodology {
  version: string;
  hash: string;
  kind: string;
  formula: string;
  thresholds: { moderate: number; high: number; critical: number };
  min_coverage_for_score: number;
  min_coverage_for_classification: number;
  min_scored_factors: number;
  categories: CategoryDoc[];
  max_possible_coverage: number;
  parameters_editable_via_api: boolean;
  disclaimer: Interpretation;
}

export interface DepSupplier {
  supplier_id: number;
  name: string;
  share: number | null;
}
export interface DepNode {
  node_type: string;
  node_id: number;
  code: string;
  name: string;
  confirmed_supplier_count: number;
  unverified_alternatives: number;
  sole_source_in_observed_dataset: boolean;
  criticality: string | null;
  share_basis: string;
  supplier_share: number | null;
  hhi: number | null;
  hhi_note: string | null;
  suppliers: DepSupplier[];
  relationship_ref: string;
}
export interface Dependencies {
  supplier_id: number;
  nodes: DepNode[];
  dependent_products: number;
  dependent_facilities: number;
  sole_sourced_count: number;
  own_relationships_by_status: Record<string, number>;
  downstream_reach: { depth: number; nodes_by_type: Record<string, number>; edges: number; truncated: boolean; reasons: string[] };
  treatment: Record<string, string>;
  notes: string[];
}

export interface CompareCell {
  supplier_id: number;
  state: FactorState;
  score: number | null;
  confidence: number | null;
  basis: string;
}
export interface Comparison {
  comparable: boolean;
  incomparable_reasons: string[];
  caveats: string[];
  suppliers: {
    supplier_id: number;
    name: string;
    supplier_code: string;
    assessment_id: number;
    status: AssessmentStatus;
    risk_score: number | null;
    classification: RiskClass | null;
    evidence_coverage: number;
    reliability: number | null;
    reliability_band: ReliabilityBand | null;
    methodology_version: string;
    computed_at: string;
  }[];
  matrix: { category: string; label: string; role: string; cells: CompareCell[] }[];
  ranking: number[];
  ranking_note: string | null;
}

export interface Region {
  region: string;
  key: string;
  suppliers: number;
  facilities: number;
  high_or_critical: number;
  unscored: number;
  verified_coordinates: number;
  missing_or_placeholder_coordinates: number;
  supplier_share: number;
  facility_share: number;
}
export interface Geography {
  regions: Region[];
  total_suppliers: number;
  total_facilities: number;
  top_region_supplier_share: number | null;
  caveats: string[];
}

export interface AlertPage {
  items: Alert[];
  total: number;
  limit: number;
  offset: number;
}
export interface HistoryPage {
  items: HistoryItem[];
  total: number;
  limit: number;
  offset: number;
  note: string;
}

export interface ReviewBody {
  assessment_id: number;
  action: ReviewAction;
  note?: string;
  factor_category?: string;
  override_classification?: RiskClass;
  override_expires_at?: string;
  target_review_id?: number;
}
