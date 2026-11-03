"""ORM event hooks: derived normalized names and append-only guards."""

from __future__ import annotations

from sqlalchemy import event

from app.models.facility import Facility
from app.models.product import Product
from app.models.registry import AuditEvent, CandidateReview
from app.models.supplier import Supplier
from app.registry.normalize import normalize_name


def _set_normalized(_mapper, _connection, target) -> None:
    target.name_normalized = normalize_name(target.name)[:255]


for _model in (Supplier, Facility, Product):
    event.listen(_model, "before_insert", _set_normalized)
    event.listen(_model, "before_update", _set_normalized)


def _forbid(_mapper, _connection, target) -> None:
    raise ValueError(f"{type(target).__name__} rows are append-only and cannot be changed or deleted")


for _model in (AuditEvent, CandidateReview):
    event.listen(_model, "before_update", _forbid)
    event.listen(_model, "before_delete", _forbid)


from app.models.risk import RiskReview  # noqa: E402

event.listen(RiskReview, "before_update", _forbid)
event.listen(RiskReview, "before_delete", _forbid)

from app.models.scenario import ScenarioHistory  # noqa: E402

event.listen(ScenarioHistory, "before_update", _forbid)
event.listen(ScenarioHistory, "before_delete", _forbid)

from app.models.impact import ImpactAlternative, ImpactCriticality, ImpactEdgeSnapshot, ImpactFinding  # noqa: E402

for _model in (ImpactFinding, ImpactEdgeSnapshot, ImpactCriticality, ImpactAlternative):  # results are insert-only
    event.listen(_model, "before_update", _forbid)
    event.listen(_model, "before_delete", _forbid)

from app.models.inventory import (  # noqa: E402
    DemandObservation,
    InventoryImportRow,
    InventoryPosition,
    InventoryReplenishment,
    StockoutFinding,
)

for _model in (
    InventoryPosition,
    DemandObservation,
    InventoryReplenishment,
    InventoryImportRow,
    StockoutFinding,
):  # insert-only
    event.listen(_model, "before_update", _forbid)
    event.listen(_model, "before_delete", _forbid)


from app.models.forecast import ForecastAlert, ForecastEvaluation, ForecastRun, ForecastValue  # noqa: E402

for _model in (ForecastValue, ForecastEvaluation, ForecastAlert):  # insert-only
    event.listen(_model, "before_update", _forbid)
    event.listen(_model, "before_delete", _forbid)

_TERMINAL = ("COMPLETED", "PARTIAL", "FAILED", "CANCELLED")


def _freeze_run(_mapper, _connection, target) -> None:
    """A forecast run may change while it is queued or running; once terminal it is immutable."""
    from sqlalchemy import inspect

    hist = inspect(target).attrs.status.history
    old = hist.deleted[0] if hist.deleted else (hist.unchanged[0] if hist.unchanged else None)
    old_v = getattr(old, "value", old)
    if old_v in _TERMINAL:
        raise ValueError("ForecastRun rows are immutable once they reach a terminal status")


def _no_run_delete(_mapper, _connection, target) -> None:
    raise ValueError("ForecastRun rows are retained and cannot be deleted")


event.listen(ForecastRun, "before_update", _freeze_run)
event.listen(ForecastRun, "before_delete", _no_run_delete)


from app.models.delivery import DeliveryAssessment, DeliveryEvent, DeliveryImportRow, DeliverySnapshot, LeadTimeRun  # noqa: E402

for _model in (DeliverySnapshot, DeliveryEvent, DeliveryImportRow, DeliveryAssessment):  # insert-only
    event.listen(_model, "before_update", _forbid)
    event.listen(_model, "before_delete", _forbid)


def _freeze_leadtime_run(_mapper, _connection, target) -> None:
    """A lead-time run may change while queued or running; once terminal it is immutable (it is a model version)."""
    from sqlalchemy import inspect

    hist = inspect(target).attrs.status.history
    old = hist.deleted[0] if hist.deleted else (hist.unchanged[0] if hist.unchanged else None)
    if getattr(old, "value", old) in _TERMINAL:
        raise ValueError("LeadTimeRun rows are immutable once they reach a terminal status")


def _no_leadtime_run_delete(_mapper, _connection, target) -> None:
    raise ValueError("LeadTimeRun rows are retained and cannot be deleted")


event.listen(LeadTimeRun, "before_update", _freeze_leadtime_run)
event.listen(LeadTimeRun, "before_delete", _no_leadtime_run_delete)


# ------------------------------------------------------------------------------- model evaluation model governance
from app.models.modelops import ModelDiagnostic, ModelEvaluation, ModelExplanation, ModelLifecycleEvent, ModelVersion  # noqa: E402, E501

for _model in (ModelLifecycleEvent, ModelExplanation, ModelDiagnostic):  # insert-only
    event.listen(_model, "before_update", _forbid)
    event.listen(_model, "before_delete", _forbid)

_VERSION_MUTABLE = {"lifecycle_state", "state_changed_at"}


def _version_guard(_mapper, _connection, target) -> None:
    """A model version never changes identity or evidence; only its lifecycle state (and the stamp) may move."""
    from sqlalchemy import inspect

    changed = {a.key for a in inspect(target).attrs if a.history.has_changes()}
    if changed - _VERSION_MUTABLE:
        raise ValueError("ModelVersion rows are immutable except for their lifecycle state")


def _no_version_delete(_mapper, _connection, target) -> None:
    raise ValueError("ModelVersion rows are retained and cannot be deleted")


event.listen(ModelVersion, "before_update", _version_guard)
event.listen(ModelVersion, "before_delete", _no_version_delete)


def _freeze_eval(_mapper, _connection, target) -> None:
    from sqlalchemy import inspect

    hist = inspect(target).attrs.status.history
    old = hist.deleted[0] if hist.deleted else (hist.unchanged[0] if hist.unchanged else None)
    if getattr(old, "value", old) in _TERMINAL:
        raise ValueError("ModelEvaluation rows are immutable once they reach a terminal status")


def _no_eval_delete(_mapper, _connection, target) -> None:
    raise ValueError("ModelEvaluation rows are retained and cannot be deleted")


event.listen(ModelEvaluation, "before_update", _freeze_eval)
event.listen(ModelEvaluation, "before_delete", _no_eval_delete)


# ------------------------------------------------------------------------------- transport network and routing
from app.models.transport import RoutingRun, TransportLane  # noqa: E402

_LANE_MUTABLE = {"status", "retired_at"}


def _lane_guard(_mapper, _connection, target) -> None:
    """A lane's figures never change in place (a new price or duration is a new lane); only availability may move."""
    from sqlalchemy import inspect

    changed = {a.key for a in inspect(target).attrs if a.history.has_changes()}
    if changed - _LANE_MUTABLE:
        raise ValueError("TransportLane rows are immutable except for their availability status")


def _no_lane_delete(_mapper, _connection, target) -> None:
    raise ValueError("TransportLane rows are retained and cannot be deleted")


event.listen(TransportLane, "before_update", _lane_guard)
event.listen(TransportLane, "before_delete", _no_lane_delete)


def _freeze_routing_run(_mapper, _connection, target) -> None:
    from sqlalchemy import inspect

    hist = inspect(target).attrs.status.history
    old = hist.deleted[0] if hist.deleted else (hist.unchanged[0] if hist.unchanged else None)
    if getattr(old, "value", old) in _TERMINAL:
        raise ValueError("RoutingRun rows are immutable once they reach a terminal status")


def _no_routing_run_delete(_mapper, _connection, target) -> None:
    raise ValueError("RoutingRun rows are retained and cannot be deleted")


event.listen(RoutingRun, "before_update", _freeze_routing_run)
event.listen(RoutingRun, "before_delete", _no_routing_run_delete)


# ------------------------------------------------------------------------------- sourcing offers and runs
from app.models.sourcing import SourcingDecision, SourcingOffer, SourcingRun  # noqa: E402

_OFFER_MUTABLE = {"status", "retired_at"}


def _offer_guard(_mapper, _connection, target) -> None:
    """An offer's terms never change in place (a new price or capacity is a new offer); only availability may move."""
    from sqlalchemy import inspect

    changed = {a.key for a in inspect(target).attrs if a.history.has_changes()}
    if changed - _OFFER_MUTABLE:
        raise ValueError("SourcingOffer rows are immutable except for their availability status")


def _no_offer_delete(_mapper, _connection, target) -> None:
    raise ValueError("SourcingOffer rows are retained and cannot be deleted")


event.listen(SourcingOffer, "before_update", _offer_guard)
event.listen(SourcingOffer, "before_delete", _no_offer_delete)


def _freeze_sourcing_run(_mapper, _connection, target) -> None:
    from sqlalchemy import inspect

    hist = inspect(target).attrs.status.history
    old = hist.deleted[0] if hist.deleted else (hist.unchanged[0] if hist.unchanged else None)
    if getattr(old, "value", old) in _TERMINAL:
        raise ValueError("SourcingRun rows are immutable once they reach a terminal status")


def _no_sourcing_run_delete(_mapper, _connection, target) -> None:
    raise ValueError("SourcingRun rows are retained and cannot be deleted")


def _decision_insert_only(_mapper, _connection, target) -> None:
    raise ValueError("SourcingDecision rows are insert-only")


event.listen(SourcingRun, "before_update", _freeze_sourcing_run)
event.listen(SourcingRun, "before_delete", _no_sourcing_run_delete)
event.listen(SourcingDecision, "before_update", _decision_insert_only)
event.listen(SourcingDecision, "before_delete", _decision_insert_only)


# ------------------------------------------------------------------- inventory optimisation
from app.models.invopt import InventoryCostProfile, ReplenishmentDecision, ReplenishmentRun  # noqa: E402


def _cost_profile_guard(_mapper, _connection, target) -> None:
    from sqlalchemy import inspect

    changed = {a.key for a in inspect(target).attrs if a.history.has_changes()}
    if changed - {"retired_at"}:
        raise ValueError("InventoryCostProfile rows are immutable except for retirement")


def _no_cost_profile_delete(_mapper, _connection, target) -> None:
    raise ValueError("InventoryCostProfile rows are retained and cannot be deleted")


def _freeze_replenishment_run(_mapper, _connection, target) -> None:
    from sqlalchemy import inspect

    hist = inspect(target).attrs.status.history
    old = hist.deleted[0] if hist.deleted else (hist.unchanged[0] if hist.unchanged else None)
    if getattr(old, "value", old) in _TERMINAL:
        raise ValueError("ReplenishmentRun rows are immutable once they reach a terminal status")


def _no_replenishment_run_delete(_mapper, _connection, target) -> None:
    raise ValueError("ReplenishmentRun rows are retained and cannot be deleted")


def _replenishment_decision_insert_only(_mapper, _connection, target) -> None:
    raise ValueError("ReplenishmentDecision rows are insert-only")


event.listen(InventoryCostProfile, "before_update", _cost_profile_guard)
event.listen(InventoryCostProfile, "before_delete", _no_cost_profile_delete)
event.listen(ReplenishmentRun, "before_update", _freeze_replenishment_run)
event.listen(ReplenishmentRun, "before_delete", _no_replenishment_run_delete)
event.listen(ReplenishmentDecision, "before_update", _replenishment_decision_insert_only)
event.listen(ReplenishmentDecision, "before_delete", _replenishment_decision_insert_only)


# ------------------------------------------------------------------- resilience optimisation
from app.models.resilience import ResilienceDecision, ResilienceRun  # noqa: E402


def _freeze_resilience_run(_mapper, _connection, target) -> None:
    from sqlalchemy import inspect

    hist = inspect(target).attrs.status.history
    old = hist.deleted[0] if hist.deleted else (hist.unchanged[0] if hist.unchanged else None)
    if getattr(old, "value", old) in _TERMINAL:
        raise ValueError("ResilienceRun rows are immutable once they reach a terminal status")


def _no_resilience_run_delete(_mapper, _connection, target) -> None:
    raise ValueError("ResilienceRun rows are retained and cannot be deleted")


def _resilience_decision_insert_only(_mapper, _connection, target) -> None:
    raise ValueError("ResilienceDecision rows are insert-only")


event.listen(ResilienceRun, "before_update", _freeze_resilience_run)
event.listen(ResilienceRun, "before_delete", _no_resilience_run_delete)
event.listen(ResilienceDecision, "before_update", _resilience_decision_insert_only)
event.listen(ResilienceDecision, "before_delete", _resilience_decision_insert_only)


# ------------------------------------------------------------------- scenario comparison and decision support
from app.models.decision import DecisionCase, DecisionCaseVersion, DecisionEvaluation, DecisionEvent  # noqa: E402

_FROZEN_CASE = ("APPROVED", "REJECTED", "SUPERSEDED")


def _guard_decision_case(_mapper, _connection, target) -> None:
    from sqlalchemy import inspect

    st = inspect(target).attrs.status.history
    old = st.deleted[0] if st.deleted else (st.unchanged[0] if st.unchanged else None)
    if old in _FROZEN_CASE and not (old in ("APPROVED", "REJECTED") and target.status == "SUPERSEDED"):
        raise ValueError(
            "A decided or superseded DecisionCase is frozen (APPROVED or REJECTED may only become SUPERSEDED)"
        )


def _no_decision_delete(_mapper, _connection, target) -> None:
    raise ValueError(f"{type(target).__name__} rows are retained and cannot be deleted")


def _decision_insert_only(_mapper, _connection, target) -> None:
    raise ValueError(f"{type(target).__name__} rows are insert-only")


event.listen(DecisionCase, "before_update", _guard_decision_case)
for _m in (DecisionCase, DecisionCaseVersion, DecisionEvaluation, DecisionEvent):
    event.listen(_m, "before_delete", _no_decision_delete)
for _m in (DecisionCaseVersion, DecisionEvaluation, DecisionEvent):
    event.listen(_m, "before_update", _decision_insert_only)

from app.models.alerting import (  # noqa: E402
    AlertEscalation,
    AlertEvent,
    AlertLink,
    AlertNote,
    AlertOccurrence,
    AlertRuleVersion,
    AlertTransition,
    NotificationAttempt,
)

for _model in (
    AlertRuleVersion,
    AlertTransition,
    AlertNote,
    AlertLink,
    AlertEscalation,
    NotificationAttempt,
    AlertOccurrence,
):
    event.listen(_model, "before_update", _forbid)
    event.listen(_model, "before_delete", _forbid)
event.listen(AlertEvent, "before_delete", _forbid)
