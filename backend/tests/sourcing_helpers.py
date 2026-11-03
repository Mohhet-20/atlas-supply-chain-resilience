"""SYNTHETIC fixtures for sourcing tests. Every supplier, price and capacity is invented; results verify the software only."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any

from app.sourcing.config import Limits, SourcingRequest
from app.sourcing.eligibility import Context
from app.sourcing.model import Candidate, Reliability

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
S = "/api/v1/sourcing"
LIM = Limits()


def cand(i: int, price: float | None = 10.0, cap: float | None = 100.0, **kw: Any) -> Candidate:
    base: dict[str, Any] = dict(
        uid=f"OF-T{i:03d}", supplier_id=i, supplier_code=f"S{i:03d}", supplier_name=f"Supplier {i}", country="Testland", supplier_status="ACTIVE",
        link="CONFIRMED", has_offer=True, offer_id=i, offer_status="ACTIVE", approval="APPROVED", unit="kg", price=price,
        currency="USD" if price is not None else None, capacity=cap, lead_days=10.0, defect_rate=0.01, observed_at=NOW, provenance="SYNTHETIC_DEMO",
        price_quality="VERIFIED", capacity_quality="VERIFIED", lead_quality="VERIFIED", quality_quality="VERIFIED", risk=0.2,
    )  # fmt: skip
    if "rel" in kw:
        v = kw.pop("rel")
        base["reliability"] = Reliability(v, 30 if v is not None else 0, None, "synthetic")
    return Candidate(**(base | kw))


def req(**kw: Any) -> SourcingRequest:
    d: dict[str, Any] = dict(product_id=1, destination_facility_id=1, quantity=60.0, unit="kg", currency="USD")
    return SourcingRequest(**(d | kw))


def ctx(r: SourcingRequest, **kw: Any) -> Context:
    return Context(req=r, now=NOW, **kw)


def brute_cost(
    items: list[tuple[float, int, int, int]], demand: int, min_n: int = 1, max_n: int | None = None
) -> float | None:
    """items = (cost, lo, hi, lot); exhaustive minimum over integer quantities."""
    choices = []
    for _, lo, hi, lot in items:
        choices.append([0] + [q for q in range(1, hi + 1) if q >= lo and q % (lot or 1) == 0])
    best = None
    for combo in itertools.product(*choices):
        used = sum(1 for c in combo if c)
        if sum(combo) != demand or used < max(1, min_n) or (max_n is not None and used > max_n):
            continue
        cost = sum(c * it[0] for c, it in zip(combo, items, strict=True))
        best = cost if best is None or cost < best else best
    return best
