"""Configuration, versions and fixed constants of the cascading impact analysis (``ci-1``)."""

from __future__ import annotations

import hashlib
import json
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

ALGORITHM_VERSION = "ci-1"

# Fixed analytical constants (documented in docs/methodology/cascading-impact.md). They are not request-controllable.
STRONGER_CONFIDENCE = 0.8
MODERATE_CONFIDENCE = 0.5
SUM_TOLERANCE_PCT = 0.5  # inbound supply shares may exceed 100 by this much before they are called conflicting
BOTTLENECK_MIN_DOWNSTREAM = 2
BOTTLENECK_MIN_PATHS = 3
MAX_CRITICALITY_NODES = 300
MAX_DISCONNECT_SAMPLE = 10
MAX_ALTERNATIVES_TOTAL = 500
MAX_EVIDENCE_PER_FINDING = 20
MAX_CONTEXT_EDGES = 2_000

SCOPE_STATEMENT = (
    "Scope: confirmed relationships in the network as read for this analysis, downstream only, within the stated depth "
    "and limits. An entity that is not listed was not reached within this scope; that does not mean it is safe."
)
REACHABILITY_NOTE = (
    "Graph reachability shows where a disruption could travel. It does not prove a shortage, a delay or a loss: ATLAS "
    "holds no inventory, demand, bill-of-materials or financial data, and none is assumed."
)
STRUCTURAL_CAVEAT = (
    "Structural candidate only: based on graph shape inside the affected subgraph. It is not an operational or "
    "financial finding and needs validation with real data."
)
ALTERNATIVE_CAVEAT = (
    "Structurally possible only. Spare capacity, product compatibility, qualification, transport access and "
    "availability are not known; nothing here is validated or recommended, and no action is taken."
)


class ImpactConfig(BaseModel):
    """Analyst-selectable settings. Every value is bounded; unknown fields are rejected (no organisation id, no code)."""

    model_config = ConfigDict(extra="forbid")

    max_paths_per_entity: int = Field(default=5, ge=1, le=10)
    max_path_length: int = Field(default=6, ge=1, le=6)
    alternatives_per_entity: int = Field(default=3, ge=0, le=5)
    include_proposed_alternatives: bool = False
    stale_after_days: int = Field(default=365, ge=30, le=3650)
    as_of_date: date | None = None  # relationships not effective on this date are excluded; None = dates not applied
    reference_date: date | None = None  # "today" for staleness; resolved at creation and stored so reruns reproduce


def config_hash(cfg: ImpactConfig) -> str:
    payload = {"algorithm": ALGORITHM_VERSION, "config": cfg.model_dump(mode="json")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
