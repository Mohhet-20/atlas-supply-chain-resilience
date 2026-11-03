"""The transportation network: immutable lanes between registered facilities, loaded and validated from the database.

Loading never changes a row. Lanes that cannot be traversed (suspended, retired, outside their effective window, or attached to a
facility that is inactive, on hold or merged away) are kept in ``excluded`` with the reason, so a missing route can always be
explained. ``Network`` objects are never mutated after construction; a disruption overlay builds a new one.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import EntityStatus
from app.models.facility import Facility
from app.models.transport import MODES, TransportLane
from app.models.types import utcnow


@dataclass(frozen=True)
class Lane:
    id: int
    uid: str
    origin: int
    dest: int
    mode: str
    distance_km: float | None = None
    duration_hours: float | None = None
    duration_quality: str = "UNKNOWN"
    cost: float | None = None
    currency: str | None = None
    cost_quality: str = "UNKNOWN"
    capacity: float | None = None
    capacity_unit: str | None = None
    reliability_prior: float | None = None
    restrictions: tuple[str, ...] = ()
    provenance: str = "USER_PROVIDED"
    observed_at: datetime | None = None
    relationship_id: int | None = None
    flags: tuple[str, ...] = ()  # data-quality flags, e.g. CONFLICTING_DUPLICATE
    degraded: tuple[str, ...] = ()  # disruption notes set by an overlay


@dataclass(frozen=True)
class Node:
    id: int
    code: str
    name: str
    country: str
    latitude: float | None
    longitude: float | None
    location_status: str
    owner_supplier_id: int | None
    status: str


@dataclass
class Network:
    lanes: dict[str, Lane]
    nodes: dict[int, Node]
    excluded: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    as_of: datetime | None = None
    closed: dict[str, str] = field(default_factory=dict)  # uid -> reason (overlay-closed lanes), for explanations
    closed_nodes: dict[int, str] = field(default_factory=dict)
    _out: dict[int, list[Lane]] | None = field(default=None, repr=False)

    @property
    def out(self) -> dict[int, list[Lane]]:
        if self._out is None:
            adj: dict[int, list[Lane]] = defaultdict(list)
            for ln in self.lanes.values():
                if ln.origin in self.closed_nodes or ln.dest in self.closed_nodes:
                    continue
                adj[ln.origin].append(ln)
            for v in adj.values():
                v.sort(key=lambda x: (x.dest, x.mode, x.id))
            self._out = dict(adj)
        return self._out

    def digest(self) -> str:
        """Content hash of the traversable lanes (every figure that can change a route), independent of load order."""
        rows = [
            [
                ln.uid,
                ln.origin,
                ln.dest,
                ln.mode,
                ln.distance_km,
                ln.duration_hours,
                ln.duration_quality,
                ln.cost,
                ln.currency,
                ln.cost_quality,
                ln.capacity,
                ln.capacity_unit,
                ln.reliability_prior,
                list(ln.restrictions),
                ln.provenance,
                ln.observed_at.isoformat() if ln.observed_at else None,
                ln.relationship_id,
            ]  # fmt: skip
            for ln in sorted(self.lanes.values(), key=lambda x: x.uid)
        ]
        return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()

    def summary(self) -> dict[str, Any]:
        modes: dict[str, int] = defaultdict(int)
        for ln in self.lanes.values():
            modes[ln.mode] += 1
        used = {ln.origin for ln in self.lanes.values()} | {ln.dest for ln in self.lanes.values()}
        return {
            "lanes": len(self.lanes), "facilities": len(used), "modes": dict(sorted(modes.items())), "excluded_lanes": len(self.excluded),
            "issues": len(self.issues), "closed_by_overlay": len(self.closed), "closed_facilities": len(self.closed_nodes),
        }  # fmt: skip


def lane_from_row(r: TransportLane) -> Lane:
    return Lane(
        id=r.id, uid=r.lane_uid, origin=r.origin_facility_id, dest=r.destination_facility_id, mode=r.mode,
        distance_km=r.distance_km, duration_hours=r.duration_hours, duration_quality=r.duration_quality, cost=r.cost_amount,
        currency=r.currency, cost_quality=r.cost_quality, capacity=r.capacity_amount, capacity_unit=r.capacity_unit,
        reliability_prior=r.reliability_prior, restrictions=tuple(sorted(r.restrictions or [])), provenance=r.provenance.value,
        observed_at=r.observed_at, relationship_id=r.relationship_id,
    )  # fmt: skip


def node_from_row(f: Facility) -> Node:
    return Node(
        id=f.id, code=f.facility_code, name=f.name, country=f.country_or_region, latitude=f.latitude, longitude=f.longitude,
        location_status=f.location_status.value, owner_supplier_id=f.owner_supplier_id, status=f.status.value,
    )  # fmt: skip


def load_network(
    session: Session, as_of: datetime | None = None, *, max_lanes: int = 5000, stale_days: int = 365
) -> Network:
    """Read the registered lanes. Raises ValueError when the network is larger than the configured bound."""
    now = as_of or utcnow()
    rows = list(session.scalars(select(TransportLane).order_by(TransportLane.id).limit(max_lanes + 1)))
    if len(rows) > max_lanes:
        raise ValueError(
            f"The transport network has more than {max_lanes} lanes; raise ATLAS_ROUTING_MAX_LANES deliberately."
        )
    ids = {r.origin_facility_id for r in rows} | {r.destination_facility_id for r in rows}
    facilities = {f.id: f for f in session.scalars(select(Facility).where(Facility.id.in_(ids)))} if ids else {}
    lanes: dict[str, Lane] = {}
    excluded: list[dict[str, Any]] = []
    for r in rows:
        why = None
        o, d = facilities.get(r.origin_facility_id), facilities.get(r.destination_facility_id)
        if r.status != "ACTIVE":
            why = r.status
        elif r.effective_from is not None and r.effective_from > now:
            why = "NOT_YET_EFFECTIVE"
        elif r.effective_to is not None and r.effective_to < now:
            why = "EXPIRED"
        else:
            for tag, f in (("ORIGIN", o), ("DESTINATION", d)):
                if f is None:
                    why = f"{tag}_MISSING"
                elif f.merged_into_id is not None:
                    why = f"{tag}_MERGED"
                elif f.status is EntityStatus.INACTIVE:
                    why = f"{tag}_INACTIVE"
                elif f.status is EntityStatus.ON_HOLD:
                    why = f"{tag}_ON_HOLD"
                if why:
                    break
        if why:
            excluded.append({"lane_uid": r.lane_uid, "reason": why})
        else:
            lanes[r.lane_uid] = lane_from_row(r)
    nodes = {i: node_from_row(f) for i, f in facilities.items()}
    net = Network(lanes=lanes, nodes=nodes, excluded=excluded, as_of=now)
    net.issues = validate_lanes(list(lanes.values()), now, stale_days)
    flagged = {u for i in net.issues if i["code"] in ("CONFLICTING_DUPLICATE", "DUPLICATE_LANE") for u in i["lanes"]}
    if flagged:
        net.lanes = {u: (replace_flags(ln, "DATA_CONFLICT") if u in flagged else ln) for u, ln in net.lanes.items()}
    return net


def replace_flags(ln: Lane, flag: str) -> Lane:
    from dataclasses import replace

    return replace(ln, flags=tuple(sorted({*ln.flags, flag})))


def validate_lanes(lanes: list[Lane], now: datetime, stale_days: int) -> list[dict[str, Any]]:
    """Data-quality findings about the lane set. None of them silently changes a figure."""
    issues: list[dict[str, Any]] = []
    groups: dict[tuple[int, int, str], list[Lane]] = defaultdict(list)
    for ln in lanes:
        groups[(ln.origin, ln.dest, ln.mode)].append(ln)
        if ln.mode not in MODES:
            issues.append(
                {
                    "code": "UNSUPPORTED_MODE",
                    "severity": "error",
                    "lanes": [ln.uid],
                    "detail": f"Mode '{ln.mode}' is not supported.",
                }
            )
        for name, v in (("distance_km", ln.distance_km), ("duration_hours", ln.duration_hours), ("cost", ln.cost)):
            if v is not None and v < 0:
                issues.append(
                    {"code": "NEGATIVE_VALUE", "severity": "error", "lanes": [ln.uid], "detail": f"{name} is negative."}
                )
        if ln.capacity is not None and ln.capacity <= 0:
            issues.append(
                {
                    "code": "INVALID_CAPACITY",
                    "severity": "error",
                    "lanes": [ln.uid],
                    "detail": "Capacity must be positive.",
                }
            )
        if (ln.cost is None) != (ln.currency is None):
            issues.append(
                {
                    "code": "CURRENCY_MISSING",
                    "severity": "error",
                    "lanes": [ln.uid],
                    "detail": "Cost and currency must be given together.",
                }
            )
        if (ln.capacity is None) != (ln.capacity_unit is None):
            issues.append(
                {
                    "code": "UNIT_MISSING",
                    "severity": "error",
                    "lanes": [ln.uid],
                    "detail": "Capacity and unit must be given together.",
                }
            )
        if ln.observed_at is None:
            issues.append(
                {
                    "code": "UNDATED",
                    "severity": "warning",
                    "lanes": [ln.uid],
                    "detail": "No observation date: freshness cannot be judged.",
                }
            )
        elif now - ln.observed_at > timedelta(days=stale_days):
            issues.append(
                {
                    "code": "STALE",
                    "severity": "warning",
                    "lanes": [ln.uid],
                    "detail": f"Figures were last confirmed more than {stale_days} days ago.",
                }
            )
    for group in groups.values():
        if len(group) > 1:
            figs = {(g.distance_km, g.duration_hours, g.cost, g.currency, g.capacity) for g in group}
            code = "CONFLICTING_DUPLICATE" if len(figs) > 1 else "DUPLICATE_LANE"
            detail = (
                "Several active lanes connect the same facilities by the same mode with different figures."
                if code == "CONFLICTING_DUPLICATE"
                else "Several active lanes connect the same facilities by the same mode with identical figures."
            )
            issues.append(
                {"code": code, "severity": "warning", "lanes": sorted(g.uid for g in group), "detail": detail}
            )
    ccy = {ln.currency for ln in lanes if ln.currency}
    if len(ccy) > 1:
        issues.append(
            {
                "code": "MIXED_CURRENCY",
                "severity": "info",
                "lanes": [],
                "detail": f"Lanes are priced in {', '.join(sorted(ccy))}; routes spanning several currencies have no total cost.",
            }
        )
    issues.sort(key=lambda i: (i["code"], i["lanes"]))
    return issues
