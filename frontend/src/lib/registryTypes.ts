/** Types mirroring the Registry Intelligence API (backend/app/api/v1/registry.py). */

export type RegistryType = "SUPPLIER" | "FACILITY" | "PRODUCT";
export type Plural = "suppliers" | "facilities" | "products";
export type Role = "viewer" | "editor" | "reviewer" | "admin";
export type CandidateTier = "STRONG" | "POSSIBLE" | "INSUFFICIENT";
export type CandidateStatus = "NEW" | "UNDER_REVIEW" | "CONFIRMED_DUPLICATE" | "REJECTED" | "DEFERRED" | "REOPENED" | "MERGED";

export const PLURAL: Record<RegistryType, Plural> = { SUPPLIER: "suppliers", FACILITY: "facilities", PRODUCT: "products" };
export const SINGULAR: Record<Plural, RegistryType> = { suppliers: "SUPPLIER", facilities: "FACILITY", products: "PRODUCT" };
export const TYPE_PLURAL_LABEL: Record<RegistryType, string> = { SUPPLIER: "Suppliers", FACILITY: "Facilities", PRODUCT: "Products" };
export const TYPE_LABEL: Record<RegistryType, string> = { SUPPLIER: "Supplier", FACILITY: "Facility", PRODUCT: "Product" };

export interface Me {
  actor: string;
  role: Role;
  authenticated: boolean;
  auth_required: boolean;
  writes_enabled: boolean;
}

export interface Overview {
  counts: Record<RegistryType, { total: number; live: number; merged: number }>;
  candidates: {
    by_status: Record<string, number>;
    awaiting_review: number;
    open_by_tier: Record<string, number>;
    confirmed_duplicates: number;
  };
  merges: { executed: number; compensated: number };
  quality: {
    has_data: boolean;
    average_overall: number | null;
    records_with_missing_required_fields: number | null;
    low_score_count: number | null;
    trend: { run_id: number; started_at: string; average_overall: number; entities: number }[];
    message: string | null;
  };
  recent_changes: { id: number; occurred_at: string; actor: string; action: string; target_type: string | null; target_id: string | null }[];
}

export interface MatchReason {
  kind: string;
  field: string;
  exact: boolean;
  detail: string;
  score?: number;
  via_merged?: string;
}

export interface SearchHit {
  type: RegistryType;
  id: number;
  code: string;
  name: string;
  status: string;
  country_or_region: string | null;
  category: string | null;
  merged_into: { type: RegistryType; id: number; code: string; name: string } | null;
  source_namespace: string | null;
  external_id: string | null;
  updated_at: string;
  version: number;
  match_reasons: MatchReason[];
  exact: boolean;
  approximate: boolean;
  best_reason: string;
}

export interface SearchResponse {
  items: SearchHit[];
  total: number;
  limit: number;
  offset: number;
  truncated: boolean;
  notes: string[];
}

export interface Alias {
  id: number;
  alias: string;
  alias_normalized: string;
  kind: string;
  origin_merge_id: number | null;
  created_by: string;
}

export interface Identifier {
  id: number | null;
  scheme: string;
  value: string;
  source_id: number | null;
  source_namespace: string | null;
  authoritative: boolean;
  primary: boolean;
  origin_merge_id: number | null;
  created_by: string | null;
}

export interface HistoryEvent {
  id: number;
  occurred_at: string;
  actor: string;
  authenticated: boolean;
  action: string;
  outcome: string;
  details: Record<string, unknown>;
}

export interface QualityIssue {
  dimension: string;
  kind: string;
  severity: "INFO" | "WARNING" | "ERROR";
  field: string | null;
  message: string;
  remediation: string;
  candidate_id?: number;
}

export interface EntityQuality {
  run_id: number;
  config_version: string;
  overall: number;
  dimensions: Record<string, { score: number; detail?: string } | number>;
  issues: QualityIssue[];
  assessed_at: string;
  entity_version: number;
  stale: boolean;
}

export interface RelationshipRef {
  id: number;
  code: string;
  target?: { id?: number; code?: string; name?: string } | null;
  [k: string]: unknown;
}

export interface EntityDetail {
  type: RegistryType;
  id: number;
  code: string;
  lifecycle: string;
  version: number;
  name: string;
  attributes: Record<string, unknown>;
  normalized: { name: string };
  source_namespace: string | null;
  aliases: Alias[];
  identifiers: Identifier[];
  relationships: Record<string, RelationshipRef[]>;
  lineage: Record<string, unknown>[];
  identity: {
    merged_into: { id: number; code: string; name: string } | null;
    merged_from: { id: number; code: string; name: string; merge_id?: number }[];
  };
  quality: EntityQuality | null;
  open_candidates: number;
  history: HistoryEvent[];
}

export interface Signal {
  signal: string;
  family: string;
  points: number;
  detail: string;
}

export interface Evidence {
  rule_version: string;
  score: number;
  score_meaning: string;
  tier: CandidateTier;
  hard_conflict: boolean;
  families: string[];
  signals: Signal[];
  thresholds: { strong: number; possible: number };
}

export interface Candidate {
  id: number;
  entity_type: RegistryType;
  left_id: number;
  right_id: number;
  left_name: string | null;
  right_name: string | null;
  rule_version: string;
  score: number;
  tier: CandidateTier;
  hard_conflict: boolean;
  status: CandidateStatus;
  evidence: Evidence;
  left_version: number;
  right_version: number;
  stale: boolean;
  first_seen_at: string;
  updated_at: string;
}

export interface CandidateList {
  items: Candidate[];
  total: number;
  limit: number;
  offset: number;
}

export interface ComparisonSide {
  id: number;
  code: string;
  version: number;
  lifecycle: string;
  provenance: string;
  source_namespace: string | null;
  external_id: string | null;
  created_at: string;
  identifiers: Identifier[];
  aliases: string[];
  relationships: Record<string, unknown[]>;
  lineage_count: number;
}

export interface CandidateComparison {
  candidate: Candidate;
  fields: {
    field: string;
    left: unknown;
    right: unknown;
    left_normalized: unknown;
    right_normalized: unknown;
    equal: boolean;
    conflict: boolean;
    missing_side: string | null;
  }[];
  left: ComparisonSide;
  right: ComparisonSide;
  history: {
    id: number;
    from_status: string | null;
    to_status: string;
    actor: string;
    authenticated: boolean;
    reason: string | null;
    created_at: string;
  }[];
}

export type Choice = "KEEP_SURVIVOR" | "USE_MERGED" | "CUSTOM";
export interface FieldDecision {
  choice: Choice;
  value?: unknown;
}

export interface MergeRequestBody {
  entity_type: RegistryType;
  survivor_id: number;
  merged_id: number;
  survivor_version: number;
  merged_version: number;
  field_decisions: Record<string, FieldDecision>;
  relationship_decisions: Record<string, string>;
  acknowledge_identifier_conflict: boolean;
  candidate_id: number | null;
  reason: string | null;
}

export interface MergePreview {
  entity_type: RegistryType;
  survivor: { id: number; code: string; name: string; version: number };
  merged: { id: number; code: string; name: string; version: number };
  fields: {
    field: string;
    columns: string[];
    survivor: unknown;
    merged: unknown;
    status: "SAME" | "FILL_FROM_MERGED" | "MERGED_EMPTY" | "CONFLICT";
    default_choice: Choice | null;
    choice: Choice | null;
    resolved_value: unknown;
    needs_decision: boolean;
  }[];
  resulting_values: Record<string, unknown>;
  relationship_impact: {
    table: string;
    row_id: number;
    code: string;
    column: string;
    old: number;
    new: number;
    action: "REASSIGN" | "CONFLICT_DUPLICATE" | "CONFLICT_SELF_LOOP";
    note?: string;
    decision?: string | null;
  }[];
  identifiers: { preserved: Identifier[]; conflicts: { scheme: string; survivor: string[]; merged: string[] }[] };
  aliases_to_add: string[];
  review: { candidate_id: number | null; status: string | null; matches_pair: boolean };
  blockers: { code: string; message: string }[];
  warnings: { code: string; message: string }[];
  can_execute: boolean;
}

export interface MergeRecord {
  id: number;
  merge_uid: string;
  entity_type: RegistryType;
  survivor_id: number;
  merged_id: number;
  status: "EXECUTED" | "COMPENSATED";
  actor: string;
  authenticated: boolean;
  reason: string | null;
  candidate_id: number | null;
  survivor_version_before: number;
  survivor_version_after: number;
  created_at: string;
  compensated_at: string | null;
  compensated_by: string | null;
  compensation_reason: string | null;
  reassigned_count: number;
  dropped_count: number;
  survivor_before?: Record<string, unknown>;
  merged_before?: Record<string, unknown>;
  field_decisions?: { rows: { field: string; status: string; choice: string; survivor: unknown; merged: unknown; resolved_value: unknown }[] };
  reassignments?: { table: string; row_id: number; column: string; old: number; new: number; code: string }[];
  removed_duplicates?: { table: string; code: string; reason: string }[];
}

export interface QualitySummary {
  has_data: boolean;
  message?: string;
  run?: {
    id: number;
    run_uid: string;
    config_version: string;
    config_hash: string;
    actor: string;
    started_at: string;
    entities_assessed: number;
    average_overall: number;
    dimension_averages: Record<string, number>;
  };
  by_entity_type?: Record<string, { count: number; average: number }>;
  by_source?: { source_id: number; namespace: string; count: number; average: number }[];
  issue_counts?: Record<string, number>;
  low_score_count?: number;
  low_score_threshold?: number;
  trend?: { run_id: number; started_at: string; average_overall: number; entities: number }[];
  disclaimer?: string;
}

export interface QualityIssueRow extends QualityIssue {
  entity_type: RegistryType;
  entity_id: number;
  entity_score: number;
  entity_name: string | null;
}

export interface QualityIssuePage {
  items: QualityIssueRow[];
  total: number;
  limit: number;
  offset: number;
  has_data: boolean;
  run_id: number | null;
}

export interface QualityConfigInfo {
  version: string;
  hash: string;
  dimensions: Record<string, string>;
  config: Record<string, unknown>;
}

export interface RunInfo {
  run_uid: string;
  status: string;
  pairs_compared: number;
  candidates_found: number;
  candidates_new: number;
  truncated_reason: string | null;
  config_version: string;
  duration_ms: number;
}

export interface AuditEvent {
  id: number;
  occurred_at: string;
  actor: string;
  actor_role: string | null;
  authenticated: boolean;
  action: string;
  target_type: string | null;
  target_id: string | null;
  outcome: string;
  request_id: string | null;
  details: Record<string, unknown>;
}
