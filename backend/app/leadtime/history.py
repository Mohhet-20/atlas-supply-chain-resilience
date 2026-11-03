"""Point-in-time supplier/lane history. A statistic at time ``t`` uses ONLY deliveries whose outcome was known strictly before ``t``.

The sweep processes outcomes in the order they became known, so a delivery can never contribute to its own features, nor
to those of any prediction made before its outcome existed.
"""

from __future__ import annotations

from bisect import insort
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SHRINK = 5.0  # pseudo-observations pulling a sparse supplier's late rate toward the pooled rate known at t


@dataclass(frozen=True)
class Outcome:
    known_at: datetime
    supplier_id: int | None
    lane: tuple[int | None, int]
    lead: float | None
    late: bool | None


@dataclass(frozen=True)
class HF:
    sup_n: int
    sup_median: float | None
    sup_late_rate: float | None  # shrunk toward the pooled rate; None when nothing is known yet
    lane_n: int
    lane_median: float | None
    pooled_n: int
    pooled_late_rate: float | None


def _median(xs: list[float]) -> float | None:
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


class Sweep:
    """Add outcomes in known_at order, query features at non-decreasing times."""

    def __init__(self, outcomes: list[Outcome]) -> None:
        self.out = sorted(outcomes, key=lambda o: o.known_at)
        self.i = 0
        self.sup_lead: dict[Any, list[float]] = {}
        self.lane_lead: dict[Any, list[float]] = {}
        self.sup_late: dict[Any, list[int]] = {}  # [late, n]
        self.pool = [0, 0]
        self.t: datetime | None = None

    def at(self, t: datetime, supplier_id: int | None, lane: tuple[int | None, int]) -> HF:
        if self.t is not None and t < self.t:
            raise ValueError("Sweep queries must be non-decreasing in time.")
        self.t = t
        while self.i < len(self.out) and self.out[self.i].known_at < t:  # strictly before
            o = self.out[self.i]
            self.i += 1
            if o.lead is not None:
                insort(self.sup_lead.setdefault(o.supplier_id, []), o.lead)
                insort(self.lane_lead.setdefault(o.lane, []), o.lead)
            if o.late is not None:
                c = self.sup_late.setdefault(o.supplier_id, [0, 0])
                c[0] += int(o.late)
                c[1] += 1
                self.pool[0] += int(o.late)
                self.pool[1] += 1
        sl, ll = self.sup_lead.get(supplier_id, []), self.lane_lead.get(lane, [])
        pooled = (self.pool[0] / self.pool[1]) if self.pool[1] else None
        late = self.sup_late.get(supplier_id, [0, 0])
        rate = ((late[0] + SHRINK * pooled) / (late[1] + SHRINK)) if pooled is not None else None
        return HF(len(sl), _median(sl), rate, len(ll), _median(ll), self.pool[1], pooled)


def history_at(outcomes: list[Outcome], t: datetime, supplier_id: int | None, lane: tuple[int | None, int]) -> HF:
    """One-off query (assessments)."""
    return Sweep([o for o in outcomes if o.known_at < t]).at(t, supplier_id, lane)
