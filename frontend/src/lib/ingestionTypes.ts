// Mirrors backend/app/schemas/ingestion.py and schemas/product.py.
import type { EntityStatus, Provenance } from "./types";

export type EntityKind = "SUPPLIER" | "FACILITY" | "PRODUCT" | "RELATIONSHIP" | "PRODUCT_LINK";
export type IngestPolicy = "ATOMIC" | "PARTIAL";
export type SourceFormat = "CSV" | "JSON" | "ANY";
export type BatchStatus = "PROCESSING" | "COMMITTED" | "COMPLETED_WITH_ERRORS" | "REJECTED" | "FAILED";
export type Severity = "INFO" | "WARNING" | "ERROR" | "CRITICAL";
export type ResolutionStatus = "OPEN" | "ACKNOWLEDGED" | "RESOLVED" | "WONT_FIX";
export type ProfileStatus = "DRAFT" | "ACTIVE" | "RETIRED";
export type RowOutcome = "CREATED" | "UPDATED" | "UNCHANGED" | "REJECTED" | "DUPLICATE_IN_BATCH" | "SKIPPED";

export const ENTITY_KINDS: EntityKind[] = ["SUPPLIER", "FACILITY", "PRODUCT", "RELATIONSHIP", "PRODUCT_LINK"];

export interface DataSource {
  id: number;
  namespace: string;
  name: string;
  source_type: string;
  provenance: Provenance;
  status: string;
  description: string | null;
  created_at: string;
}

export interface FieldInfo {
  name: string;
  kind: string;
  required: boolean;
  description: string;
}

export interface FormatsResponse {
  formats: { format: string; content_types: string[]; description: string }[];
  entity_types: Record<EntityKind, FieldInfo[]>;
  limits: { max_upload_bytes: number; max_rows: number; max_json_depth: number };
  encodings: string[];
  delimiters: string[];
  policies: IngestPolicy[];
  normalization_version: string;
}

export interface ColumnMapping {
  source: string;
  transform?: string[];
  default?: string | number | null;
  value_map?: Record<string, string>;
}

export interface ProfileDefinition {
  columns: Record<string, ColumnMapping>;
  unknown_field_policy: "ignore" | "warn" | "reject";
  date_format: string;
  null_tokens: string[];
  unit_rules: Record<string, { unit_field: string; target_unit: string; factors: Record<string, number> }>;
}

export interface MappingProfile {
  id: number;
  name: string;
  version: number;
  entity_type: EntityKind;
  source_format: SourceFormat;
  source_id: number | null;
  status: ProfileStatus;
  definition: ProfileDefinition;
  definition_hash: string;
  description: string | null;
  created_at: string;
  activated_at: string | null;
}

export interface Batch {
  id: number;
  batch_uid: string;
  status: BatchStatus;
  policy: IngestPolicy;
  source_id: number;
  source_namespace: string;
  entity_type: EntityKind;
  source_format: SourceFormat;
  mapping_profile_ref: string;
  mapping_profile_id: number | null;
  file_name: string | null;
  content_sha256: string;
  content_bytes: number;
  fingerprint: string;
  normalization_version: string;
  total_rows: number;
  created_count: number;
  updated_count: number;
  unchanged_count: number;
  rejected_count: number;
  skipped_count: number;
  duplicate_count: number;
  finding_count: number;
  failure_reason: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface CommitResponse {
  ok: boolean;
  replay: boolean;
  batch: Batch;
}

export interface RowResult {
  id: number;
  batch_id: number;
  row_index: number;
  source_line: number | null;
  external_id: string | null;
  outcome: RowOutcome;
  entity_id: number | null;
  record_fingerprint: string | null;
  message: string | null;
}

export interface Finding {
  id: number;
  finding_key: string;
  rule_id: string;
  severity: Severity;
  batch_id: number | null;
  source_id: number | null;
  entity_type: EntityKind | null;
  external_id: string | null;
  field: string | null;
  row_index: number | null;
  source_line: number | null;
  message: string;
  suggestion: string | null;
  observed_value: string | null;
  resolution_status: ResolutionStatus;
  resolution_note: string | null;
  resolved_at: string | null;
  created_at: string;
}

export interface Rule {
  rule_id: string;
  name: string;
  severity: Severity;
  description: string;
}

export interface Lineage {
  id: number;
  entity_type: EntityKind;
  entity_id: number;
  batch_id: number;
  batch_uid: string | null;
  source_namespace: string | null;
  external_id: string;
  row_index: number;
  source_line: number | null;
  mapping_profile_ref: string;
  normalization_version: string;
  record_fingerprint: string;
  outcome: RowOutcome;
  changed_fields: string[] | null;
  file_name: string | null;
  ingested_at: string;
}

export interface InspectResult {
  format: string;
  encoding: string;
  delimiter: string | null;
  columns: string[];
  row_count: number;
  sample_rows: Record<string, string | null>[];
  file_problems: { rule_id: string; severity: Severity; message: string; suggestion: string | null }[];
  suggested_columns: Record<string, { source: string }>;
  unmapped_columns: string[];
  canonical_fields: FieldInfo[];
}

export interface PreviewFinding {
  rule_id: string;
  severity: Severity;
  row_index: number | null;
  source_line: number | null;
  external_id: string | null;
  field: string | null;
  message: string;
  suggestion: string | null;
  observed_value: string | null;
}

export interface PreviewResponse {
  persisted: boolean;
  fingerprint: string;
  plan_digest: string;
  policy: IngestPolicy;
  entity_type: EntityKind;
  source_format: SourceFormat;
  source_namespace: string;
  mapping_profile_ref: string;
  normalization_version: string;
  file_name: string | null;
  content_sha256: string;
  content_bytes: number;
  would_apply: boolean;
  already_applied_batch: string | null;
  counts: { total_rows: number; to_create: number; to_update: number; unchanged: number; rejected: number; duplicates: number };
  finding_counts: Record<string, number>;
  findings: PreviewFinding[];
  findings_truncated: boolean;
  rows: { row_index: number; source_line: number | null; external_id: string | null; action: string; changed_fields: string[]; messages: string[] }[];
  rows_truncated: boolean;
  columns: string[];
}

export interface IngestionSummary {
  sources: number;
  profiles: number;
  active_profiles: number;
  batches: number;
  batches_by_status: Record<string, number>;
  open_findings: number;
  open_findings_by_severity: Record<string, number>;
  rows_processed: number;
}

export interface Product {
  id: number;
  product_code: string;
  name: string;
  category: string | null;
  unit_of_measure: string | null;
  status: EntityStatus;
  provenance: Provenance;
  source_namespace: string | null;
  external_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProductLink {
  id: number;
  link_code: string;
  link_type: "SUPPLIES_PRODUCT" | "PRODUCES" | "STORES";
  product_code: string;
  product_name: string;
  supplier_code: string | null;
  supplier_name: string | null;
  facility_code: string | null;
  facility_name: string | null;
  quantity_value: number | null;
  quantity_unit: string | null;
  lead_time_days: number | null;
  effective_from: string | null;
  effective_to: string | null;
  provenance: Provenance;
}

export interface ProductDetail extends Product {
  links: ProductLink[];
}
