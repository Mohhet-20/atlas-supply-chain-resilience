"""Context for route evaluation, read from existing ATLAS evidence: measured lane reliability and facility risk.

Nothing here estimates a value. A lane gets a MEASURED reliability only when the delivery history has at least the minimum number
of completed deliveries with a known promise on exactly that origin, destination (and mode); otherwise it falls back to the lane's
documented prior, or stays unknown.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.leadtime import semantics as sem
from app.leadtime.analytics import wilson
from app.leadtime.resolve import load_deliveries
from app.models.risk import RiskAssessment
from app.routing.evaluate import Context, Rel
from app.routing.network import Network


def measured(
    session: Session, net: Network, now: datetime, min_n: int, max_rows: int
) -> tuple[dict[str, Rel], list[str]]:
    warnings: list[str] = []
    pairs = {(ln.origin, ln.dest) for ln in net.lanes.values()}
    if not pairs:
        return {}, warnings
    deliveries = load_deliveries(session, as_of=now, limit=max_rows)
    if len(deliveries) > max_rows:
        deliveries = deliveries[:max_rows]
        warnings.append(
            f"Delivery history was truncated at {max_rows} records; measured reliability may use fewer observations than exist."
        )
    per_pair: dict[tuple[int, int], list[Any]] = defaultdict(list)
    for dl in deliveries:
        if dl.origin_id is not None and (dl.origin_id, dl.destination_id) in pairs:
            per_pair[(dl.origin_id, dl.destination_id)].append(dl)
    lanes_by_pair: dict[tuple[int, int], list[Any]] = defaultdict(list)
    for ln in net.lanes.values():
        lanes_by_pair[(ln.origin, ln.dest)].append(ln)
    out: dict[str, Rel] = {}
    for pair, ds in per_pair.items():
        lanes = lanes_by_pair[pair]
        for ln in lanes:
            n = late = 0
            for dl in ds:
                mode = (dl.mode or "").upper() or None
                if (mode is not None and mode != ln.mode) or (mode is None and len(lanes) > 1):
                    continue
                state, _ = sem.classify(dl, now, 0.0)
                if state != sem.COMPLETED:
                    continue
                lt = sem.is_late(dl, "ORIGINAL", 0.0)
                if lt is None:
                    continue
                n += 1
                late += 1 if lt else 0
            if n >= min_n:
                k = n - late
                out[ln.uid] = Rel(
                    k / n,
                    "MEASURED",
                    n,
                    wilson(k, n),
                    "on-time share of completed deliveries against the original promise",
                )
            elif n:
                out[ln.uid] = Rel(
                    None, "UNKNOWN", n, None, f"only {n} usable deliveries (minimum {min_n}); not used as a measurement"
                )
    return out, warnings


def facility_risk(session: Session, net: Network) -> dict[int, float | None]:
    owners = {n.id: n.owner_supplier_id for n in net.nodes.values() if n.owner_supplier_id}
    scores: dict[int, float] = {}
    if owners:
        for sid, score in session.execute(
            select(RiskAssessment.supplier_id, RiskAssessment.risk_score).where(
                RiskAssessment.supplier_id.in_(set(owners.values())),
                RiskAssessment.is_current.is_(True),
                RiskAssessment.risk_score.is_not(None),
            )
        ):
            scores[sid] = float(score) / 100.0
    return {fid: scores.get(owners.get(fid, -1)) for fid in net.nodes}


def build_context(
    session: Session, net: Network, *, use_measured: bool, min_n: int, max_rows: int, now: datetime
) -> tuple[Context, list[str]]:
    rel: dict[str, Rel] = {}
    warnings: list[str] = []
    meas: dict[str, Rel] = {}
    if use_measured:
        meas, warnings = measured(session, net, now, min_n, max_rows)
    for uid, ln in net.lanes.items():
        m = meas.get(uid)
        if m is not None and m.value is not None:
            rel[uid] = m
        elif ln.reliability_prior is not None:
            rel[uid] = Rel(
                ln.reliability_prior,
                "PRIOR",
                m.n if m else None,
                None,
                "documented prior (assumed, not measured)" + (f"; {m.note}" if m and m.note else ""),
            )
        else:
            rel[uid] = m or Rel(None, "UNKNOWN", None, None, "no measurement and no documented prior")
    return Context(rel=rel, risk=facility_risk(session, net), now=now), warnings
