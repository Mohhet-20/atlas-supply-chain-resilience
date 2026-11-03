export type Severity = "INFO" | "WARNING" | "ERROR" | "CRITICAL";
export type FindingStatus = "OPEN" | "ACKNOWLEDGED" | "IN_REMEDIATION" | "RESOLVED" | "ACCEPTED_EXCEPTION" | "WONT_FIX" | "REOPENED";
export type FindingAction = "acknowledge" | "start_remediation" | "resolve" | "accept_exception" | "reopen";
export type QEntityKind = "SUPPLIER" | "FACILITY" | "PRODUCT" | "RELATIONSHIP" | "PRODUCT_LINK";
export type ScoreStateName = "SCORED" | "INSUFFICIENT_EVIDENCE" | "NOT_EVALUATED";

export const DIMENSIONS = [
  "completeness",
  "validity",
  "consistency",
  "uniqueness",
  "freshness",
  "referential_integrity",
  "relationship_quality",
  "provenance_completeness",
] as const;

export interface Finding {
  id: number;
  origin: string;
  rule_id: string;
  rule_name: string | null;
  rule_version: number | null;
  dimension: string | null;
  severity: Severity;
  status: FindingStatus;
  entity_type: QEntityKind | null;
  entity_id: number | null;
  entity_name: string | null;
  external_id: string | null;
  field: string | null;
  message: string;
  suggestion: string | null;
  evidence: Record<string, unknown> | null;
  requires_review: boolean;
  batch_id: number | null;
  row_index: number | null;
  source_id: number | null;
  source_namespace: string | null;
  run_id: number | null;
  first_detected_at: string | null;
  last_evaluated_at: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
  resolution_note: string | null;
  assigned_to: string | null;
  reopened_count: number;
  version: number;
  fix_action: string | null;
}
export interface FindingPage {
  items: Finding[];
  total: number;
  limit: number;
  offset: number;
}
export interface FindingEvent {
  id: number;
  occurred_at: string;
  actor: string;
  action: string;
  from_status: string | null;
  to_status: string | null;
  note: string | null;
}
export interface FindingDetail {
  finding: Finding;
  rule: { rule_id: string; name: string; version: number; description: string; remediation: string; fix_action: string | null } | null;
  events: FindingEvent[];
}

export interface Aggregate {
  subjects: number;
  scored: number;
  insufficient_evidence: number;
  not_evaluated: number;
  coverage: number | null;
  mean: number | null;
  min: number | null;
  distribution: Record<string, number> | null;
  state: string;
}
export interface ScoreInfo {
  state: ScoreStateName;
  score: number | null;
  message?: string | null;
  checks_total?: number;
  checks_failed?: number;
  exceptions_excluded?: number;
  breakdown?: { dimensions: Record<string, { units: number; failed: number; score: number | null }>; missing_units: number; invalid_units: number } | null;
  catalog_hash?: string;
  formula_version?: string;
  evaluated_at?: string;
}
export interface RunInfo {
  id: number;
  run_uid: string;
  scope_type: string;
  scope_ref: Record<string, unknown> | null;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "PARTIAL" | "FAILED";
  trigger: string;
  requested_by: string;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  examined: Record<string, number>;
  checks_evaluated: number | null;
  findings: Record<string, number>;
  subjects_failed: number | null;
  subjects_skipped: number | null;
  truncated: boolean | null;
  truncation_reasons: string[];
  error: string | null;
  failures?: unknown[] | null;
  catalog_hash?: string | null;
}
export interface Counts {
  total: number;
  open: number;
  by_status: Record<string, number>;
  open_by_severity: Record<string, number>;
  open_by_dimension: Record<string, number>;
  open_by_entity_type: Record<string, number>;
  critical_or_high_open: number;
  open_requires_review: number;
  accepted_exceptions: number;
}
export interface Overview {
  has_evaluation: boolean;
  latest_run: RunInfo | null;
  latest_network_run: RunInfo | null;
  scores: { overall: Aggregate; by_subject_type: Record<string, Aggregate> } | null;
  findings: Counts;
  relationships_with_issues: number;
  batches_with_warnings: { id: number; batch_uid: string; file_name: string | null; status: string; total_rows: number; rejected: number; findings: number }[];
  totals: Record<string, number>;
  disclaimer: string;
  empty_message: string | null;
}
export interface SourceReport {
  source: { id: number; namespace: string; name: string; type: string; provenance: string };
  indicators: Record<string, number | null>;
  elevated_concern: boolean;
  concern_reasons: string[];
  method: string;
}
export interface Analytics {
  trend: { run_id: number; finished_at: string | null; mean: number | null; coverage: number | null; comparable_with_previous: boolean | null }[];
  stale_distribution: { fresh_days: number; stale_days: number; by_entity_type: Record<string, { total: number; fresh: number; aging: number; stale: number }> };
  time_to_resolve_hours: { n: number; median: number | null; mean: number | null };
  repeated_failures: { top_rules_by_open_findings: { rule_id: string; rule_name: string | null; open_findings: number }[] };
  relationship_quality_distribution: Aggregate;
  definitions: Record<string, string>;
}
export interface RelatedRel {
  ref: string;
  entity_type: QEntityKind;
  entity_id: number;
  relationship_type: string;
  status: string;
  score: ScoreInfo;
  open_findings: number;
}
export interface EntityQuality {
  entity: { type: QEntityKind; id: number; name: string; status: string; [k: string]: unknown };
  score: ScoreInfo;
  open_findings: Finding[];
  other_findings: Finding[];
  history: { run_id: number; state: string; score: number | null; evaluated_at: string; comparable_with_latest: boolean }[];
  source_records: { batch_id: number; external_id: string; row_index: number; outcome: string; changed_fields: string[] | null; ingested_at: string }[];
  related_relationships: RelatedRel[];
}
export interface Lineage {
  layers_present: string[];
  has_source_record: boolean;
  limitations: string[];
  ingestion: { batch_id: number; external_id: string; row_index: number; mapping_profile_ref: string; record_fingerprint: string; outcome: string; changed_fields: string[] | null; ingested_at: string }[];
  audit: { id: number; occurred_at: string; actor: string; action: string }[];
  merges: { merge_uid: string; role: string; status: string; reason: string }[];
  edge_events: { occurred_at: string; action: string; actor: string; from_status: string | null; to_status: string | null }[];
}
export interface BatchFinding {
  id: number;
  rule_id: string;
  severity: Severity;
  field: string | null;
  message: string;
  suggestion: string | null;
  observed_value: string | null;
  observed_value_redacted: boolean;
}
export interface BatchReport {
  batch: { id: number; batch_uid: string; file_name: string | null; status: string; entity_type: string; policy: string; total_rows: number; source_namespace: string | null; failure_reason: string | null };
  counts: Record<string, number>;
  all_rows_accounted_for: boolean;
  file_level_findings: BatchFinding[];
  records_evaluated: Aggregate;
  rows: {
    items: { row_index: number; source_line: number | null; external_id: string | null; outcome: string; classification: string; message: string | null; entity_id: number | null; findings: BatchFinding[] }[];
    total: number;
    limit: number;
    offset: number;
  };
  classification_rules: string;
  raw_payload: string;
}
export interface RuleInfo {
  rule_id: string;
  name: string;
  version: number;
  description: string;
  dimension: string;
  severity: Severity;
  entity_types: string[];
  enabled: boolean;
  default_enabled: boolean;
  requires_review: boolean;
  remediation: string;
  fix_action: string | null;
  override: { reason: string | null; updated_by: string; updated_at: string } | null;
}
export interface RunPage {
  items: RunInfo[];
  total: number;
  limit: number;
  offset: number;
}
export interface FindingFilters {
  status?: string;
  min_severity?: string;
  dimension?: string;
  entity_type?: string;
  q?: string;
  rule_id?: string;
  batch_id?: number;
  source_id?: number;
  entity_id?: number;
  sort?: string;
  limit?: number;
  offset?: number;
}
