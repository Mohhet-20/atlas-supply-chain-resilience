"""Domain enumerations shared by ORM models, schemas and services."""

from __future__ import annotations

from enum import Enum


class Provenance(str, Enum):
    """Origin of a record or value. Never collapsed or inferred in the UI."""

    SYNTHETIC_DEMO = "SYNTHETIC_DEMO"
    USER_PROVIDED = "USER_PROVIDED"
    IMPORTED_EXTERNAL = "IMPORTED_EXTERNAL"
    MODEL_ESTIMATE = "MODEL_ESTIMATE"
    SIMULATED = "SIMULATED"


class EntityStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ON_HOLD = "ON_HOLD"


class FacilityType(str, Enum):
    MANUFACTURING = "MANUFACTURING"
    WAREHOUSE = "WAREHOUSE"
    DISTRIBUTION = "DISTRIBUTION"
    CROSS_DOCK = "CROSS_DOCK"


class RelationshipType(str, Enum):
    SUPPLIES = "SUPPLIES"  # supplier -> facility
    TRANSFERS_TO = "TRANSFERS_TO"  # facility -> facility


class EntityType(str, Enum):
    SUPPLIER = "SUPPLIER"
    FACILITY = "FACILITY"


class SourceType(str, Enum):
    CSV_FILE = "CSV_FILE"
    JSON_FILE = "JSON_FILE"
    API = "API"
    SYNTHETIC_GENERATOR = "SYNTHETIC_GENERATOR"
    MANUAL = "MANUAL"
    EXTERNAL_SYSTEM = "EXTERNAL_SYSTEM"  # ERP/WMS/TMS/procurement adapters (future)


class SourceStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class EntityKind(str, Enum):
    """Entity types that can be ingested or referenced by lineage and findings."""

    SUPPLIER = "SUPPLIER"
    FACILITY = "FACILITY"
    PRODUCT = "PRODUCT"
    RELATIONSHIP = "RELATIONSHIP"  # supply_relationships: SUPPLIES / TRANSFERS_TO
    PRODUCT_LINK = "PRODUCT_LINK"  # product_links: SUPPLIES_PRODUCT / PRODUCES / STORES


class ProductLinkType(str, Enum):
    SUPPLIES_PRODUCT = "SUPPLIES_PRODUCT"  # supplier -> product
    PRODUCES = "PRODUCES"  # facility -> product
    STORES = "STORES"  # facility -> product


class LocationStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"


class IngestPolicy(str, Enum):
    ATOMIC = "ATOMIC"
    PARTIAL = "PARTIAL"


class BatchStatus(str, Enum):
    PROCESSING = "PROCESSING"
    COMMITTED = "COMMITTED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class RowOutcome(str, Enum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    REJECTED = "REJECTED"
    DUPLICATE_IN_BATCH = "DUPLICATE_IN_BATCH"
    SKIPPED = "SKIPPED"  # not applied because the atomic batch was rejected


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class ResolutionStatus(str, Enum):
    """Finding lifecycle. WONT_FIX is the legacy spelling of an accepted exception and stays valid."""

    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_REMEDIATION = "IN_REMEDIATION"
    RESOLVED = "RESOLVED"
    ACCEPTED_EXCEPTION = "ACCEPTED_EXCEPTION"
    REOPENED = "REOPENED"
    WONT_FIX = "WONT_FIX"


class QualityDimension(str, Enum):
    COMPLETENESS = "completeness"
    VALIDITY = "validity"
    CONSISTENCY = "consistency"
    UNIQUENESS = "uniqueness"
    FRESHNESS = "freshness"
    REFERENTIAL_INTEGRITY = "referential_integrity"
    RELATIONSHIP_QUALITY = "relationship_quality"
    PROVENANCE_COMPLETENESS = "provenance_completeness"


class FindingOrigin(str, Enum):
    INGESTION = "INGESTION"  # raised while a file was imported (rules DQ001-DQ020)
    EVALUATION = "EVALUATION"  # raised by a quality evaluation run (rules QR1xx / QR2xx)


class EvalScope(str, Enum):
    ENTITY = "ENTITY"
    RELATIONSHIPS = "RELATIONSHIPS"
    BATCH = "BATCH"
    SOURCE = "SOURCE"
    NETWORK = "NETWORK"


class EvalStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"  # finished, but some subjects failed or the scope limit truncated the run
    FAILED = "FAILED"


class ScoreState(str, Enum):
    SCORED = "SCORED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ProfileStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class SourceFormat(str, Enum):
    CSV = "CSV"
    JSON = "JSON"
    ANY = "ANY"


class RegistryType(str, Enum):
    """Master-data entity types managed by the registry (relationships/links are not mergeable entities)."""

    SUPPLIER = "SUPPLIER"
    FACILITY = "FACILITY"
    PRODUCT = "PRODUCT"


class AliasKind(str, Enum):
    TRADING_NAME = "TRADING_NAME"
    ALIAS = "ALIAS"
    FORMER_NAME = "FORMER_NAME"
    MERGED_NAME = "MERGED_NAME"  # name of a record merged into this one
    SOURCE_CODE = "SOURCE_CODE"  # source-specific code that is not an authoritative identifier


class CandidateStatus(str, Enum):
    NEW = "NEW"
    UNDER_REVIEW = "UNDER_REVIEW"
    CONFIRMED_DUPLICATE = "CONFIRMED_DUPLICATE"
    REJECTED = "REJECTED"  # reviewed and judged to be distinct entities
    DEFERRED = "DEFERRED"
    REOPENED = "REOPENED"
    MERGED = "MERGED"  # terminal: the pair has been merged


class CandidateTier(str, Enum):
    STRONG = "STRONG"
    POSSIBLE = "POSSIBLE"
    INSUFFICIENT = "INSUFFICIENT"


class MergeStatus(str, Enum):
    EXECUTED = "EXECUTED"
    COMPENSATED = "COMPENSATED"


class RunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    TRUNCATED = "TRUNCATED"  # a configured bound was reached; results are partial
    FAILED = "FAILED"


class AuditOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    DENIED = "DENIED"
    FAILED = "FAILED"


class EdgeStatus(str, Enum):
    """Lifecycle of a network relationship. Only CONFIRMED relationships are part of the default network view."""

    PROPOSED = "PROPOSED"  # asserted but not yet reviewed
    CONFIRMED = "CONFIRMED"  # reviewed (or loaded from a trusted source) and in force
    REJECTED = "REJECTED"  # reviewed and found not to hold; kept as evidence
    INACTIVE = "INACTIVE"  # was in force, no longer is; history preserved


class EdgeCriticality(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------------------------------- supplier risk


class RiskCategory(str, Enum):
    """Supplier-risk categories (see docs/methodology/risk-rules.md). A category existing is not evidence."""

    CONCENTRATION = "CONCENTRATION"
    DEPENDENCY = "DEPENDENCY"
    GEOGRAPHIC = "GEOGRAPHIC"
    OPERATIONAL = "OPERATIONAL"
    DELIVERY = "DELIVERY"
    FINANCIAL = "FINANCIAL"
    COMPLIANCE = "COMPLIANCE"
    DATA_QUALITY = "DATA_QUALITY"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    RELATIONSHIP_UNCERTAINTY = "RELATIONSHIP_UNCERTAINTY"


class FactorState(str, Enum):
    SCORED = "SCORED"
    UNAVAILABLE = "UNAVAILABLE"  # required evidence is missing: never treated as low risk
    NOT_APPLICABLE = "NOT_APPLICABLE"  # the question does not arise for this supplier (excluded from coverage)


class FactorBasis(str, Enum):
    OBSERVED = "OBSERVED"  # computed directly from a recorded value
    INFERRED = "INFERRED"  # computed with a labelled analytical approximation
    UNKNOWN = "UNKNOWN"  # nothing to compute from


class Freshness(str, Enum):
    FRESH = "FRESH"
    AGING = "AGING"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class AssessmentStatus(str, Enum):
    ASSESSED = "ASSESSED"  # enough coverage for a score and a classification
    PARTIAL = "PARTIAL"  # a score exists but coverage is below the classification threshold
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # no score: unknown is not low risk


class RiskClass(str, Enum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ReliabilityBand(str, Enum):
    HIGH = "HIGH"
    MODERATE = "MODERATE"
    LOW = "LOW"


class RiskRunScope(str, Enum):
    SUPPLIER = "SUPPLIER"
    SUPPLIERS = "SUPPLIERS"
    NETWORK = "NETWORK"


class ReviewAction(str, Enum):
    ACKNOWLEDGE = "ACKNOWLEDGE"
    NOTE = "NOTE"
    OVERRIDE = "OVERRIDE"
    REVOKE_OVERRIDE = "REVOKE_OVERRIDE"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"
    FOLLOW_UP = "FOLLOW_UP"


class RiskAlertType(str, Enum):
    SCORE_INCREASE = "SCORE_INCREASE"
    NEW_SINGLE_SOURCE = "NEW_SINGLE_SOURCE"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    RELATIONSHIP_UNVERIFIED = "RELATIONSHIP_UNVERIFIED"
    ASSESSMENT_UNAVAILABLE = "ASSESSMENT_UNAVAILABLE"
    METHODOLOGY_CHANGE = "METHODOLOGY_CHANGE"


class RiskAlertStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    DISMISSED = "DISMISSED"
    RESOLVED = "RESOLVED"


class EvidenceType(str, Enum):
    REGISTRY_FIELD = "REGISTRY_FIELD"
    RELATIONSHIP = "RELATIONSHIP"
    QUALITY_FINDING = "QUALITY_FINDING"
    QUALITY_SCORE = "QUALITY_SCORE"
    OBSERVATION = "OBSERVATION"
    IMPORT_RECORD = "IMPORT_RECORD"
    ANALYST_NOTE = "ANALYST_NOTE"  # reserved: analyst notes are reviews, not factor evidence, in rm-1
    EXTERNAL_OBSERVATION = "EXTERNAL_OBSERVATION"  # reserved: no external connector exists
    MODEL_OUTPUT = "MODEL_OUTPUT"  # reserved: no scenario model exists yet


# ------------------------------------------------------------------------------------ disruption scenarios


class ScenarioStatus(str, Enum):
    DRAFT = "DRAFT"  # being edited; cannot be run
    READY = "READY"  # validated; can be run
    ARCHIVED = "ARCHIVED"  # read-only; run history kept


class ScenarioType(str, Enum):
    SUPPLIER = "SUPPLIER"
    FACILITY = "FACILITY"
    RELATIONSHIP = "RELATIONSHIP"
    REGIONAL = "REGIONAL"  # an explicitly selected set of entities that share a region label
    MULTI_ENTITY = "MULTI_ENTITY"
    CUSTOM = "CUSTOM"


class EventType(str, Enum):
    SUPPLIER_UNAVAILABLE = "SUPPLIER_UNAVAILABLE"
    SUPPLIER_CAPACITY_REDUCTION = "SUPPLIER_CAPACITY_REDUCTION"
    FACILITY_SHUTDOWN = "FACILITY_SHUTDOWN"
    FACILITY_CAPACITY_REDUCTION = "FACILITY_CAPACITY_REDUCTION"
    RELATIONSHIP_UNAVAILABLE = "RELATIONSHIP_UNAVAILABLE"
    LEAD_TIME_INCREASE = "LEAD_TIME_INCREASE"
    REGION_DISRUPTION = "REGION_DISRUPTION"
    CUSTOM = "CUSTOM"


class EventValidation(str, Enum):
    UNVALIDATED = "UNVALIDATED"
    VALID = "VALID"
    WARNING = "WARNING"
    INVALID = "INVALID"


class SimStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"  # finished, but a bound truncated the analysis or part of it failed
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ImpactKind(str, Enum):
    DIRECT = "DIRECT"  # the entity is a named target of an event
    DOWNSTREAM = "DOWNSTREAM"  # reached by following material flow away from a disrupted entity
    UPSTREAM = "UPSTREAM"  # a supplier of a disrupted entity (loses a customer); qualitative only


class Quantification(str, Enum):
    QUANTIFIED = "QUANTIFIED"  # every needed input was known
    LOWER_BOUND = "LOWER_BOUND"  # only part of the affected inflow had a known share
    UNQUANTIFIED = "UNQUANTIFIED"  # exposure is real but the data does not support a number
