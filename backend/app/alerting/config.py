"""Alerting constants and schemas: lifecycle, severities, event catalogue, detector catalogue and rule validation.

Rules are *data*: a detector key from a closed catalogue plus validated parameters. Nothing here evaluates user-supplied expressions, SQL or code.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ENGINE_VERSION = "al-1"
EVENT_SCHEMA_VERSION = "ev-1"
SUPPORTED_SCHEMA_VERSIONS = ("ev-1",)

SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
CHANNELS = ("in_app", "webhook")

# Valid transitions. closed is terminal (a recurrence after closure is a NEW alert); resolved may be reopened.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("acknowledged", "escalated", "resolved", "suppressed"),
    "acknowledged": ("investigating", "escalated", "resolved", "suppressed"),
    "investigating": ("escalated", "resolved"),
    "escalated": ("acknowledged", "investigating", "resolved"),
    "suppressed": ("open",),
    "resolved": ("closed", "open"),
    "closed": (),
}

LIMITS = {
    "payload_bytes": 8192,
    "entities": 50,
    "matches_per_rule": 500,
    "rules_per_evaluation": 100,
    "events_page": 100,
    "note_chars": 2000,
}

# Keys never stored or logged from payloads, whatever the event says.
SENSITIVE = ("password", "secret", "token", "authorization", "api_key", "apikey", "credential", "cookie")

EVENT_TYPES: dict[str, dict[str, Any]] = {
    "metric.observation": {
        "label": "A measured value reported by an external system",
        "required": {"metric": "str", "value": "number", "unit": "str"},
        "optional": {"basis": "str"},
        "basis": "observed",
    },
    "disruption.reported": {
        "label": "A disruption reported by a source (not a scenario)",
        "required": {"title": "str"},
        "optional": {"severity": "str", "expected_duration_days": "number"},
        "basis": "observed",
    },
    "source.failure": {
        "label": "A source system reported a failure",
        "required": {"reason": "str"},
        "optional": {},
        "basis": "observed",
    },
}
PROVENANCES = ("USER_PROVIDED", "IMPORTED_EXTERNAL", "SYNTHETIC_DEMO")
ENTITY_TYPES = ("supplier", "facility", "product", "lane", "delivery", "source", "scenario", "run", "product_facility")

OPERATORS = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b}
OP_TEXT = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}

P = dict[str, Any]


def _num(lo: float, hi: float, default: float | None = None, req: bool = False, unit: str | None = None) -> P:
    return {"type": "number", "min": lo, "max": hi, "default": default, "required": req, "unit": unit}


def _int(lo: int, hi: int, default: int | None = None, req: bool = False, unit: str | None = None) -> P:
    return {"type": "int", "min": lo, "max": hi, "default": default, "required": req, "unit": unit}


def _choice(choices: tuple[str, ...], default: str | None = None, req: bool = False) -> P:
    return {"type": "choice", "choices": choices, "default": default, "required": req}


def _choices(choices: tuple[str, ...], default: list[str] | None = None) -> P:
    return {"type": "choices", "choices": choices, "default": default, "required": False}


def _text(default: str | None = None, req: bool = False, maxlen: int = 64) -> P:
    return {"type": "text", "default": default, "required": req, "max": maxlen}


RUN_KINDS = ("simulation", "resilience", "sourcing", "replenishment", "forecast", "stockout", "impact")
OP = _choice(tuple(OPERATORS), "gte")

DETECTORS: dict[str, dict[str, Any]] = {
    "supplier_risk_score": {
        "label": "Supplier risk score at or above a threshold",
        "source": "supplier_risk",
        "basis": "assessed",
        "mode": "pull",
        "available": True,
        "meaning": "The current rule-based supplier-risk score (0-100) of each active supplier. It is a heuristic over stored evidence, not a prediction of an incident.",
        "missing": "A supplier whose assessment has no score (insufficient evidence) is counted as no-data and never matched; assessments below the evidence-coverage minimum are skipped and counted.",
        "params": {
            "operator": OP,
            "threshold": _num(0, 100, req=True, unit="score"),
            "critical_value": _num(0, 100),
            "min_evidence_coverage": _num(0, 1, 0.0, unit="fraction"),
        },
    },
    "stockout_status": {
        "label": "Inventory shortage or stockout risk",
        "source": "inventory",
        "basis": "observed|predicted",
        "mode": "pull",
        "available": True,
        "meaning": "Findings of the latest completed baseline stockout analysis. current_shortage is observed from recorded positions; projected_stockout and low_coverage are projections and are labelled predicted.",
        "missing": "No completed baseline analysis, or one older than max_age_days, is no-data: stale findings never raise alerts.",
        "params": {
            "statuses": _choices(
                ("current_shortage", "projected_stockout", "low_coverage"), ["current_shortage", "projected_stockout"]
            ),
            "min_confidence": _choice(("LOW", "MEDIUM", "HIGH"), "LOW"),
            "max_age_days": _int(1, 90, 14, unit="days"),
        },
    },
    "delivery_late_risk": {
        "label": "Predicted late-delivery risk at or above a threshold",
        "source": "delivery",
        "basis": "predicted",
        "mode": "pull",
        "available": True,
        "meaning": "The model late-risk score (0-1) of delivery assessments issued in the window. Assessments whose score is only a historical late rate (not a per-delivery model) are excluded. Uncalibrated scores are labelled as such in the evidence.",
        "missing": "Assessments with no score, or unavailable assessments, are no-data.",
        "params": {"threshold": _num(0, 1, req=True, unit="score"), "window_days": _int(1, 90, 7, unit="days")},
    },
    "scenario_impact": {
        "label": "Scenario analysis exposes many entities (hypothetical)",
        "source": "disruption",
        "basis": "hypothetical",
        "mode": "pull",
        "available": True,
        "meaning": "Completed impact analyses of scenarios. A scenario is a what-if: these alerts say a hypothetical disruption would reach many entities, never that a disruption is happening. Reach is structural and is not a loss.",
        "missing": "Analyses without a summary are no-data.",
        "params": {
            "metric": _choice(
                ("facilities_exposed", "suppliers_exposed", "products_exposed", "unique_entities"),
                "facilities_exposed",
                True,
            ),
            "threshold": _int(1, 100000, req=True, unit="entities"),
            "window_days": _int(1, 90, 14, unit="days"),
        },
    },
    "source_ingest_failures": {
        "label": "Repeated failed or rejected ingestion batches for a source",
        "source": "data_quality",
        "basis": "observed",
        "mode": "pull",
        "available": True,
        "meaning": "Failed or rejected ingestion batches per data source in the window. One alert per source, never one per invalid row.",
        "missing": "A source without batches in the window is not matched.",
        "params": {"min_failures": _int(1, 1000, 2, unit="batches"), "window_days": _int(1, 30, 7, unit="days")},
    },
    "source_stale": {
        "label": "Active data source has not delivered a batch recently",
        "source": "data_quality",
        "basis": "observed",
        "mode": "pull",
        "available": True,
        "meaning": "Time since the newest finished ingestion batch of each ACTIVE data source.",
        "missing": "A source that has never delivered a batch is no-data (staleness is unknown), not stale.",
        "params": {"max_age_hours": _int(1, 24 * 365, req=True, unit="hours")},
    },
    "run_outcome": {
        "label": "Analysis run failed technically or produced no feasible plan",
        "source": "processing",
        "basis": "observed",
        "mode": "pull",
        "available": True,
        "meaning": "failed: the run's status is FAILED (a technical failure). infeasible: the run COMPLETED but its result status says infeasible, timed out or had insufficient data (the plan is operationally infeasible; the software worked).",
        "missing": "Runs without a result status are not matched as infeasible.",
        "params": {
            "condition": _choice(("failed", "infeasible"), "failed", True),
            "kinds": _choices(RUN_KINDS, list(RUN_KINDS)),
            "window_days": _int(1, 30, 1, unit="days"),
        },
    },
    "forecast_monitoring": {
        "label": "Forecast monitoring signals",
        "source": "forecast",
        "basis": "assessed",
        "mode": "pull",
        "available": True,
        "meaning": "Existing forecast monitoring signals (deterioration, sustained bias, stale forecast, input-quality change), de-duplicated by their stored fingerprint.",
        "missing": "No signals in the window means no match.",
        "params": {
            "severities": _choices(("INFO", "WARNING"), ["WARNING"]),
            "window_days": _int(1, 90, 7, unit="days"),
        },
    },
    "event_threshold": {
        "label": "Reported metric crosses a threshold",
        "source": "event",
        "basis": "observed",
        "mode": "event",
        "available": True,
        "meaning": "A metric.observation event whose value compares true against the threshold. The event's unit must equal the rule's unit.",
        "missing": "Events with another metric name, a different unit or a non-numeric value never match.",
        "params": {
            "metric": _text(req=True),
            "operator": OP,
            "threshold": _num(-1e12, 1e12, req=True),
            "unit": _text(req=True, maxlen=24),
            "critical_value": _num(-1e12, 1e12),
        },
    },
    "reported_disruption": {
        "label": "A disruption reported by a source affects entities",
        "source": "disruption",
        "basis": "observed",
        "mode": "event",
        "available": True,
        "meaning": "A disruption.reported event naming at least min_affected ATLAS entities. Reported by a source; ATLAS has not verified it independently, and the event's provenance is shown. SYNTHETIC_DEMO events are ignored unless allowed.",
        "missing": "An event naming no entities is not matched.",
        "params": {
            "min_affected": _int(1, 50, 1, unit="entities"),
            "allow_synthetic": {"type": "bool", "default": False, "required": False},
        },
    },
    "capacity_pressure": {
        "label": "Capacity utilisation or demand-to-capacity above a limit",
        "source": "capacity",
        "basis": "observed",
        "mode": "pull",
        "available": False,
        "meaning": "Would compare demand with available capacity.",
        "missing": "",
        "unavailable_reason": "ATLAS stores no capacity-utilisation or available-capacity signal (only supplier capacity figures on relationships). Capacity pressure is not inferred from demand forecasts alone, so this detector is unavailable.",
        "params": {},
    },
}
UNAVAILABLE_CHANNELS = {"email": "No mail transport is configured or implemented in this deployment."}


class Notify(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channels: list[Literal["in_app", "webhook"]] = Field(default_factory=lambda: ["in_app"], max_length=2)
    recipients: list[Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")]] = (
        Field(default_factory=list, max_length=20)
    )
    min_severity: Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"
    webhook: Annotated[str | None, StringConstraints(max_length=48, pattern=r"^[A-Za-z0-9._-]+$")] = None


class RuleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(min_length=3, max_length=120, strip_whitespace=True)]
    description: Annotated[str, StringConstraints(max_length=500)] = ""
    detector: str
    params: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
    enabled: bool = True
    dedup_window_minutes: int = Field(default=1440, ge=0, le=43200)
    cooldown_minutes: int = Field(default=60, ge=0, le=10080)
    auto_resolve: bool = False
    notify: Notify = Field(default_factory=Notify)
    default_assignee: Annotated[str | None, StringConstraints(max_length=64)] = None
    escalation_policy: Annotated[str | None, StringConstraints(max_length=40)] = None
    effective_from: str | None = None
    effective_to: str | None = None


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(min_length=3, max_length=120, strip_whitespace=True)]
    enabled: bool = True
    ack_minutes: int = Field(ge=1, le=60 * 24 * 14)
    resolve_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 90)
    escalate_after_minutes: int = Field(ge=1, le=60 * 24 * 14)
    targets: list[Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")]] = (
        Field(min_length=1, max_length=5)
    )


def validate_params(detector: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Return the detector's parameters with defaults applied, or raise ValueError listing every problem."""
    spec = DETECTORS.get(detector)
    if spec is None:
        raise ValueError(f"unknown detector '{detector}'")
    if not spec["available"]:
        raise ValueError(spec["unavailable_reason"])
    errs: list[str] = []
    out: dict[str, Any] = {}
    for k in raw:
        if k not in spec["params"]:
            errs.append(f"params.{k}: not a parameter of {detector}")
    for k, p in spec["params"].items():
        if k not in raw or raw[k] is None:
            if p.get("required"):
                errs.append(f"params.{k}: required")
            elif p.get("default") is not None:
                out[k] = p["default"]
            continue
        v, t = raw[k], p["type"]
        if t in ("number", "int"):
            if isinstance(v, bool) or not isinstance(v, int | float) or (t == "int" and int(v) != v):
                errs.append(f"params.{k}: must be {'an integer' if t == 'int' else 'a number'}")
            elif not p["min"] <= v <= p["max"]:
                errs.append(
                    f"params.{k}: must be between {p['min']} and {p['max']}{' ' + p['unit'] if p.get('unit') else ''}"
                )
            else:
                out[k] = int(v) if t == "int" else float(v)
        elif t == "choice":
            if v not in p["choices"]:
                errs.append(f"params.{k}: must be one of {', '.join(p['choices'])}")
            else:
                out[k] = v
        elif t == "choices":
            if not isinstance(v, list) or not v or any(x not in p["choices"] for x in v) or len(set(v)) != len(v):
                errs.append(f"params.{k}: must be a non-empty list of distinct values from {', '.join(p['choices'])}")
            else:
                out[k] = list(v)
        elif t == "text":
            if not isinstance(v, str) or not v.strip() or len(v) > p["max"]:
                errs.append(f"params.{k}: must be non-empty text up to {p['max']} characters")
            else:
                out[k] = v.strip()
        elif t == "bool":
            if not isinstance(v, bool):
                errs.append(f"params.{k}: must be true or false")
            else:
                out[k] = v
    if "critical_value" in out and "threshold" in out and "operator" in out:
        up = out["operator"] in ("gt", "gte")
        if (up and out["critical_value"] < out["threshold"]) or (not up and out["critical_value"] > out["threshold"]):
            errs.append(
                "params.critical_value: contradicts the threshold (it must be at least as extreme as the threshold in the same direction)"
            )
    if "critical_value" in out and "threshold" in out and "operator" not in out:
        if out["critical_value"] < out["threshold"]:
            errs.append("params.critical_value: must not be below the threshold")
    if errs:
        raise ValueError("; ".join(errs))
    return out
