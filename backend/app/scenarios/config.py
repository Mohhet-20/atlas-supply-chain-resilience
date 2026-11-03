"""Simulation configuration (``sc-1``) and the engine version.

Everything that can change a result is here and is hashed into every run. These are analytical choices, not measurements:
see docs/methodology/scenario-simulation.md. Server limits (events, depth, nodes, edges, time, concurrency) come from
settings and can never be raised by a request.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ENGINE_VERSION = "sim-1"
CONFIG_VERSION = "sc-1"

# Exposure levels are labels over the *known* inflow share at risk. They are not probabilities and not severities.
HIGH_LOSS_PCT = 50.0  # known loss share at or above this is labelled HIGH
MAX_PARENT_EDGES_STORED = 10
MAX_ALTERNATIVES_PER_ENTITY = 10
MAX_ALTERNATIVES_TOTAL = 500
MAX_PATH_STEPS = 12
RESULT_CAP = 2_000  # persisted impacted entities per run
EXPLAIN_NOTE = (
    "Potential exposure from a hypothetical scenario, computed deterministically from confirmed relationships. "
    "It is not an observed event, a forecast, or a probability."
)


class Assumptions(BaseModel):
    """Analyst-reviewed assumptions stored on the scenario. Unknown values stay unknown."""

    model_config = ConfigDict(extra="forbid")

    downstream_depth: int = Field(3, ge=1, le=6)
    upstream_depth: int = Field(1, ge=0, le=6)
    include_upstream: bool = True
    include_ownership: bool = False  # follow derived OWNS edges (qualitative only)
    include_alternatives: bool = True
    include_proposed_alternatives: bool = True  # PROPOSED links may be listed as alternatives, labelled as unconfirmed
    as_of_date: date | None = None  # None: relationship effective dates are not applied
    assume_no_buffer: bool = True  # no inventory or substitution is modelled; this must stay True in sc-1
    notes: str | None = Field(None, max_length=1000)


def default_assumptions() -> dict[str, Any]:
    return Assumptions().model_dump(mode="json")


def config_payload() -> dict[str, Any]:
    return {
        "engine_version": ENGINE_VERSION, "config_version": CONFIG_VERSION, "high_loss_pct": HIGH_LOSS_PCT,
        "max_parent_edges": MAX_PARENT_EDGES_STORED, "max_alt_per_entity": MAX_ALTERNATIVES_PER_ENTITY,
        "max_alt_total": MAX_ALTERNATIVES_TOTAL, "result_cap": RESULT_CAP,
        "overlap_rule": "max", "share_cap": 100.0, "back_edges": "ignored", "stores_edges": "qualitative",
    }  # fmt: skip


def config_hash() -> str:
    return hashlib.sha256(json.dumps(config_payload(), sort_keys=True).encode()).hexdigest()
