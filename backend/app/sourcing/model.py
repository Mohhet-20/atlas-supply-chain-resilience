"""In-memory candidate record: one supplier-product-destination relationship with its commercial terms and evidence.

Candidates are immutable. A scenario or what-if creates modified COPIES (``dataclasses.replace``); stored offers, suppliers and
risk records are never touched.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Reliability:
    value: float | None = None  # measured on-time share, None when there is not enough evidence
    n: int = 0
    interval: tuple[float, float] | None = None
    note: str = ""


@dataclass(frozen=True)
class Candidate:
    uid: str  # offer uid, or "LINK-<supplier_id>" when only a relationship exists
    supplier_id: int
    supplier_code: str
    supplier_name: str
    country: str
    supplier_status: str  # ACTIVE | INACTIVE | ON_HOLD
    merged: bool = False
    link: str = "NONE"  # CONFIRMED | PROPOSED | INACTIVE | REJECTED | NONE
    has_offer: bool = False
    offer_id: int | None = None
    offer_status: str | None = None
    approval: str = "UNKNOWN"
    unit: str | None = None
    price: float | None = None
    currency: str | None = None
    freight: float | None = None
    capacity: float | None = None
    capacity_observed_at: datetime | None = None
    moq: float | None = None
    lot: float | None = None
    max_allocation: float | None = None
    lead_days: float | None = None
    lead_std_days: float | None = None
    defect_rate: float | None = None
    contract_from: datetime | None = None
    contract_to: datetime | None = None
    observed_at: datetime | None = None
    provenance: str | None = None
    price_quality: str = "UNKNOWN"
    capacity_quality: str = "UNKNOWN"
    lead_quality: str = "UNKNOWN"
    quality_quality: str = "UNKNOWN"
    restrictions: tuple[str, ...] = ()
    risk: float | None = None  # current supplier risk score as 0..1
    risk_source: str | None = None
    reliability: Reliability = field(default_factory=Reliability)
    transport: dict[str, Any] | None = None
    unavailable_reason: str | None = None  # set by a disruption overlay
    notes: tuple[str, ...] = ()  # what an overlay changed on this copy

    @property
    def effective_capacity(self) -> float | None:
        if self.capacity is None:
            return None
        return min(self.capacity, self.max_allocation) if self.max_allocation is not None else self.capacity

    def terms(self) -> dict[str, Any]:
        """The inputs the optimizer used, in a stable JSON-safe form (stored as the run snapshot)."""
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        d["restrictions"] = list(self.restrictions)
        d["notes"] = list(self.notes)
        if d["reliability"].get("interval"):
            d["reliability"]["interval"] = list(d["reliability"]["interval"])
        return d
