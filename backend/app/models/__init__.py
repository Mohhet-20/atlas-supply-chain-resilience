"""ORM models. Import all models here so Alembic metadata is complete."""

from app.models import events  # noqa: E402,F401  (registers ORM hooks)
from app.models.facility import Facility
from app.models.forecast import ForecastAlert, ForecastEvaluation, ForecastRun, ForecastValue  # noqa: F401
from app.models.graph import GraphEdgeEvent
from app.models.impact import ImpactAlternative, ImpactAnalysis, ImpactCriticality, ImpactEdgeSnapshot, ImpactFinding
from app.models.ingestion import (
    DataQualityFinding,
    IngestionBatch,
    IngestionRowResult,
    MappingProfile,
    RecordLineage,
)
from app.models.inventory import (  # noqa: F401
    DemandObservation,
    InventoryImportBatch,
    InventoryImportRow,
    InventoryPolicy,
    InventoryPosition,
    InventoryReplenishment,
    StockoutAnalysis,
    StockoutFinding,
)
from app.models.modelops import ModelDiagnostic, ModelEvaluation, ModelExplanation, ModelLifecycleEvent, ModelVersion  # noqa: F401
from app.models.observation import Observation
from app.models.product import Product, ProductLink
from app.models.quality import QualityEvalRun, QualityFindingEvent, QualityRuleSetting, QualityScore
from app.models.registry import (
    AuditEvent,
    CandidateReview,
    DuplicateCandidate,
    EntityAlias,
    EntityIdentifier,
    MergeRecord,
    QualityAssessment,
    QualityRun,
    ResolutionRun,
)
from app.models.relationship import SupplyRelationship
from app.models.risk import RiskAlert, RiskAssessment, RiskEvidence, RiskFactor, RiskReview, RiskRun
from app.models.scenario import (
    Scenario,
    ScenarioEvent,
    ScenarioHistory,
    SimulationAlternative,
    SimulationEdgeImpact,
    SimulationImpact,
    SimulationRun,
)
from app.models.source import DataSource
from app.models.supplier import Supplier
from app.models.transport import RoutingRun, TransportLane  # noqa: F401

__all__ = [
    "ImpactAlternative",
    "ImpactAnalysis",
    "ImpactCriticality",
    "ImpactEdgeSnapshot",
    "ImpactFinding",
    "GraphEdgeEvent",
    "AuditEvent",
    "CandidateReview",
    "DuplicateCandidate",
    "EntityAlias",
    "EntityIdentifier",
    "MergeRecord",
    "QualityAssessment",
    "QualityEvalRun",
    "QualityFindingEvent",
    "QualityRuleSetting",
    "QualityScore",
    "QualityRun",
    "ResolutionRun",
    "DataQualityFinding",
    "DataSource",
    "Facility",
    "IngestionBatch",
    "IngestionRowResult",
    "MappingProfile",
    "Observation",
    "Product",
    "ProductLink",
    "RecordLineage",
    "RiskAlert",
    "RiskAssessment",
    "RiskEvidence",
    "RiskFactor",
    "RiskReview",
    "RiskRun",
    "Scenario",
    "ScenarioEvent",
    "ScenarioHistory",
    "SimulationAlternative",
    "SimulationEdgeImpact",
    "SimulationImpact",
    "SimulationRun",
    "Supplier",
    "SupplyRelationship",
]
from app.models.alerting import (  # noqa: F401,E402
    Alert,
    AlertEscalation,
    AlertEscalationPolicy,
    AlertEvent,
    AlertLink,
    AlertNote,
    AlertOccurrence,
    AlertRule,
    AlertRuleRun,
    AlertRuleVersion,
    AlertSuppression,
    AlertTransition,
    InboxNotification,
    NotificationAttempt,
    NotificationDelivery,
    NotificationPreference,
)
from app.models.decision import DecisionCase, DecisionCaseVersion, DecisionEvaluation, DecisionEvent  # noqa: F401,E402
from app.models.delivery import (  # noqa: F401
    DeliveryAssessment,
    DeliveryEvent,
    DeliveryImportBatch,
    DeliveryImportRow,
    DeliverySnapshot,
    LeadTimeRun,
)
from app.models.identity import AuthSession, LoginThrottle, Organization, PasswordResetToken, User  # noqa: F401
from app.models.integrations import (  # noqa: F401
    ApiKey,
    ExportJob,
    InboundEvent,
    Integration,
    IntegrationRun,
    ServiceAccount,
    SyncCheckpoint,
    SyncError,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.models.invopt import InventoryCostProfile, ReplenishmentDecision, ReplenishmentRun  # noqa: F401,E402
from app.models.resilience import ResilienceDecision, ResilienceRun  # noqa: F401,E402
from app.models.sourcing import SourcingDecision, SourcingOffer, SourcingRun  # noqa: F401,E402
