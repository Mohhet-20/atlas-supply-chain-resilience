"""Decision support constants, request schemas and disclosure statements for the scenario comparison and decision-support layer (algorithm ``dc-1``)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ALGORITHM_VERSION = "dc-1"
METRIC_REGISTRY_VERSION = "mr-1"
EVIDENCE_KINDS = ("RESILIENCE_RUN", "SOURCING_RUN", "SIMULATION_RUN")
ROLES = ("BASELINE", "ALTERNATIVE", "SCENARIO_EXPOSURE")
PROBABILITY_BASES = ("EXPERT_DEFINED", "HISTORICAL_FREQUENCY", "MODEL_ESTIMATE")
MAINTAIN_BASELINE = "MAINTAIN_BASELINE"
EVAL_STATUSES = ("completed", "completed_with_warnings", "insufficient_data", "invalid_input")

STATEMENTS = {
    "decision_support": "A decision case compares results that already exist in ATLAS and records a human review. Approving it is an internal decision record; "
    "ATLAS never places an order, reroutes a shipment, changes a supplier allocation or edits inventory.",
    "evidence": "Every number comes from a finished run of an earlier module and is frozen into the evaluation when it is made. Nothing is estimated here; a metric that "
    "no attached run provides is shown as unavailable, never as zero.",
    "baseline": "The baseline is the one plan you designate. A cost-minimal reference plan, a supplied current allocation and a run with no disruption are different things and "
    "are labelled as what they are; they are not interchangeable.",
    "tradeoff": "A response that improves some metrics and worsens others is a trade-off, not a winner. ATLAS lists both sides and does not rank responses overall.",
    "probability": "A probability-weighted result is produced only if every disrupted condition has a probability, they sum to 1 and you named their basis. "
    "Otherwise the range and worst case are shown and nothing is called an expected value.",
    "tenancy": "Data is separated by organisation: cases are visible to users of the caller's organisation at or above the endpoint role. Requests naming an organisation are rejected.",
}


@dataclass(frozen=True)
class Limits:
    max_evidence: int = 12
    max_events_per_case: int = 500
    max_evaluations_per_case: int = 50
    max_impact_rows: int = 200
    max_scenario_refs: int = 8


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


class EvidenceRef(BaseModel):
    """One finished run to read. ``plan`` picks which plan of a resilience run is meant (an alternative id, or REFERENCE for its baseline reference plan)."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["RESILIENCE_RUN", "SOURCING_RUN", "SIMULATION_RUN"]
    run_uid: str = Field(min_length=4, max_length=40, pattern=r"^[A-Za-z0-9-]+$")
    role: Literal["BASELINE", "ALTERNATIVE", "SCENARIO_EXPOSURE"]
    plan: str | None = Field(default=None, min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    label: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def _check(self) -> EvidenceRef:
        if self.kind == "SIMULATION_RUN" and self.role != "SCENARIO_EXPOSURE":
            raise ValueError("A scenario simulation run measures exposure; give it the role SCENARIO_EXPOSURE")
        if self.kind != "SIMULATION_RUN" and self.role == "SCENARIO_EXPOSURE":
            raise ValueError("SCENARIO_EXPOSURE applies only to a SIMULATION_RUN")
        if self.plan is not None and self.kind != "RESILIENCE_RUN":
            raise ValueError("plan applies only to a RESILIENCE_RUN")
        return self


class ConditionProbability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition: str = Field(min_length=1, max_length=80)
    probability: float = Field(gt=0, le=1)


class CaseDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=200)
    purpose: str = Field(min_length=3, max_length=2000)
    description: str | None = Field(default=None, max_length=4000)
    horizon_days: int | None = Field(default=None, ge=1, le=3650)
    scenario_ids: list[int] = Field(default_factory=list, max_length=8)  # Scenarios this case is about (provenance)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=40)
    probabilities: list[ConditionProbability] = Field(default_factory=list, max_length=20)
    probability_basis: Literal["EXPERT_DEFINED", "HISTORICAL_FREQUENCY", "MODEL_ESTIMATE"] | None = None
    assumptions: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _check(self) -> CaseDefinition:
        keys = [(e.kind, e.run_uid, e.plan or "") for e in self.evidence]
        if len(set(keys)) != len(keys):
            raise ValueError("The same run and plan is listed twice")
        if self.probabilities and not self.probability_basis:
            raise ValueError("Probabilities need a probability_basis (who or what they come from)")
        if len({p.condition for p in self.probabilities}) != len(self.probabilities):
            raise ValueError("A condition has two probabilities")
        if any(len(a) > 500 or not a.strip() for a in self.assumptions):
            raise ValueError("Each assumption must be 1-500 characters")
        return self

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def digest(self) -> str:
        return stable_hash(self.canonical())
