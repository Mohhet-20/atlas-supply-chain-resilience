"""Read side: build candidates from existing ATLAS records and evidence (nothing here estimates a value).

Sources: ``sourcing_offers`` (commercial terms), ``product_links`` (the supplier-product relationship and its review status),
``suppliers`` (status, country), ``risk_assessments`` (current supplier risk), delivery history (measured on-time
reliability, lead-time, only with enough completed deliveries) and, optionally, the transport network (is a registered
route available - presence and best transit time only, never cost or lead-time arithmetic).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.leadtime import semantics as sem
from app.leadtime.analytics import wilson
from app.leadtime.resolve import load_deliveries
from app.models.enums import EdgeStatus, EntityStatus, ProductLinkType
from app.models.facility import Facility
from app.models.product import Product, ProductLink
from app.models.risk import RiskAssessment
from app.models.sourcing import SourcingOffer
from app.models.supplier import Supplier
from app.sourcing.config import Limits, SourcingRequest
from app.sourcing.eligibility import Context
from app.sourcing.model import Candidate, Reliability

_LINK_RANK = {"CONFIRMED": 0, "PROPOSED": 1, "INACTIVE": 2, "REJECTED": 3}


def _aware(d: datetime | None) -> datetime | None:
    return d


def measured_reliability(
    session: Session, req: SourcingRequest, supplier_ids: set[int], now: datetime, min_n: int, max_rows: int
) -> tuple[dict[int, Reliability], list[str]]:
    warnings: list[str] = []
    if not supplier_ids:
        return {}, warnings
    rows = load_deliveries(
        session, as_of=now, product_id=req.product_id, destination_id=req.destination_facility_id, limit=max_rows + 1
    )
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        warnings.append(
            f"Delivery history was truncated at {max_rows} records; measured reliability may use fewer observations than exist."
        )
    per: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for dl in rows:
        if dl.supplier_id not in supplier_ids:
            continue
        state, _ = sem.classify(dl, now, 0.0)
        if state != sem.COMPLETED:
            continue
        late = sem.is_late(dl, "ORIGINAL", 0.0)
        if late is None:
            continue
        per[dl.supplier_id][0] += 1
        per[dl.supplier_id][1] += 0 if late else 1
    out: dict[int, Reliability] = {}
    for sid in supplier_ids:
        n, ok = per.get(sid, [0, 0])
        if n >= min_n:
            out[sid] = Reliability(
                ok / n, n, wilson(ok, n), "on-time share of completed deliveries against the original promise"
            )
        else:
            out[sid] = Reliability(None, n, None, f"{n} usable deliveries (minimum {min_n}); not used as a measurement")
    return out, warnings


def risk_scores(session: Session, supplier_ids: set[int]) -> dict[int, tuple[float, str]]:
    if not supplier_ids:
        return {}
    out: dict[int, tuple[float, str]] = {}
    for sid, score, uid in session.execute(
        select(RiskAssessment.supplier_id, RiskAssessment.risk_score, RiskAssessment.assessment_uid).where(
            RiskAssessment.supplier_id.in_(supplier_ids),
            RiskAssessment.is_current.is_(True),
            RiskAssessment.risk_score.is_not(None),
        )
    ):
        out[sid] = (float(score) / 100.0, uid)
    return out


def transport_status(
    session: Session, supplier_ids: set[int], dest_id: int, now: datetime, limits: Limits
) -> dict[int, dict[str, Any]]:
    """Reachability and best transit time from any facility owned by the supplier to the destination, over registered lanes."""
    from app.routing.algorithms import dijkstra
    from app.routing.network import load_network

    net = load_network(session, now, max_lanes=5000, stale_days=limits.stale_days)
    out: dict[int, dict[str, Any]] = {}
    if not net.lanes:
        return {s: {"status": "NOT_EVALUATED", "note": "no transport lanes are registered"} for s in supplier_ids}
    adj: dict[int, list[Any]] = defaultdict(list)
    for ln in sorted(net.lanes.values(), key=lambda x: x.id):
        adj[ln.origin].append(ln)
    owned: dict[int, list[int]] = defaultdict(list)
    for n in net.nodes.values():
        if n.owner_supplier_id in supplier_ids:
            owned[n.owner_supplier_id].append(n.id)
    for sid in supplier_ids:
        starts = owned.get(sid, [])
        if not starts:
            out[sid] = {"status": "NOT_EVALUATED", "note": "the supplier has no registered facility to route from"}
            continue
        best: float | None = None
        for s in sorted(starts):
            if s == dest_id:
                best = 0.0
                break
            if dest_id not in net.nodes:
                continue
            path = dijkstra(adj, s, dest_id, lambda ln: ln.duration_hours)
            if path is not None:
                h = sum(x.duration_hours or 0.0 for x in path)
                best = h if best is None else min(best, h)
        out[sid] = (
            {
                "status": "REACHABLE",
                "best_transit_hours": best,
                "note": "presence of a registered route and its summed transit time only; not added to lead time or cost",
            }
            if best is not None
            else {
                "status": "NOT_REACHABLE",
                "note": "no registered route from the supplier's facilities reaches the destination",
            }
        )
    return out


def load_candidates(
    session: Session, req: SourcingRequest, limits: Limits, now: datetime
) -> tuple[list[Candidate], Context, list[str], bool]:
    warnings: list[str] = []
    product = session.get(Product, req.product_id)
    dest = session.get(Facility, req.destination_facility_id)
    pok, pnote = True, ""
    if (
        product is None
        or product.retired_at is not None
        or product.merged_into_id is not None
        or product.status is not EntityStatus.ACTIVE
    ):
        pok, pnote = False, "the product is retired, merged or not active"
    dok, dnote = True, ""
    if dest is None or dest.merged_into_id is not None or dest.status is not EntityStatus.ACTIVE:
        dok, dnote = False, "the destination facility is not active"
    offers = list(
        session.scalars(
            select(SourcingOffer)
            .where(
                SourcingOffer.product_id == req.product_id,
                SourcingOffer.destination_facility_id == req.destination_facility_id,
                SourcingOffer.status.in_(("ACTIVE", "SUSPENDED")),
            )
            .order_by(SourcingOffer.id)
        )
    )
    links: dict[int, str] = {}
    for sid, st, eff_to in session.execute(
        select(ProductLink.supplier_id, ProductLink.status, ProductLink.effective_to).where(
            ProductLink.product_id == req.product_id,
            ProductLink.link_type == ProductLinkType.SUPPLIES_PRODUCT,
            ProductLink.supplier_id.is_not(None),
        )
    ):
        name = st.value if isinstance(st, EdgeStatus) else str(st)
        if name == "CONFIRMED" and eff_to is not None and eff_to < now.date():
            name = "INACTIVE"
        if sid not in links or _LINK_RANK.get(name, 9) < _LINK_RANK.get(links[sid], 9):
            links[sid] = name
    sids = {o.supplier_id for o in offers} | set(links)
    suppliers = {s.id: s for s in session.scalars(select(Supplier).where(Supplier.id.in_(sids)))} if sids else {}
    risks = risk_scores(session, sids)
    rel, rw = (
        measured_reliability(session, req, sids, now, limits.min_reliability_sample, limits.max_delivery_rows)
        if req.use_measured_reliability
        else ({}, [])
    )
    warnings += rw
    cands: list[Candidate] = []
    by_supplier: dict[int, list[SourcingOffer]] = defaultdict(list)
    for o in offers:
        by_supplier[o.supplier_id].append(o)
    for sid in sorted(sids):
        s = suppliers.get(sid)
        if s is None:
            continue
        base = dict(
            supplier_id=sid, supplier_code=s.supplier_code, supplier_name=s.name, country=s.country_or_region, supplier_status=s.status.value,
            merged=s.merged_into_id is not None, link=links.get(sid, "NONE"), risk=risks.get(sid, (None, None))[0], risk_source=risks.get(sid, (None, None))[1],
            reliability=rel.get(sid, Reliability(None, 0, None, "delivery evidence was not requested" if not req.use_measured_reliability else "no deliveries")),
        )  # fmt: skip
        mine = by_supplier.get(sid, [])
        if not mine:
            cands.append(Candidate(uid=f"LINK-{sid}", **base))
        for o in mine:
            cands.append(
                Candidate(
                    uid=o.offer_uid, has_offer=True, offer_id=o.id, offer_status=o.status, approval=o.approval_status, unit=o.unit, price=o.unit_price,
                    currency=o.currency, freight=o.freight_per_unit, capacity=o.capacity_amount, capacity_observed_at=o.capacity_observed_at,
                    moq=o.min_order_quantity, lot=o.lot_size, max_allocation=o.max_allocation, lead_days=o.lead_time_days, lead_std_days=o.lead_time_std_days,
                    defect_rate=o.defect_rate, contract_from=o.contract_valid_from, contract_to=o.contract_valid_to, observed_at=o.observed_at,
                    provenance=o.provenance.value, price_quality=o.price_quality, capacity_quality=o.capacity_quality, lead_quality=o.lead_time_quality,
                    quality_quality=o.quality_quality, restrictions=tuple(o.restrictions or ()), **base,
                )
            )  # fmt: skip
    truncated = False
    cands.sort(key=lambda c: (c.supplier_code, c.uid))
    if len(cands) > limits.max_candidates:
        truncated = True
        warnings.append(
            f"{len(cands)} candidate relationships exist; only the first {limits.max_candidates} (by supplier code) were evaluated. Narrow the filters to see the rest."
        )
        cands = cands[: limits.max_candidates]
    if req.include_transport or req.constraints.require_transport_route:
        ts = transport_status(session, {c.supplier_id for c in cands}, req.destination_facility_id, now, limits)
        cands = [Candidate(**{**c.__dict__, "transport": ts.get(c.supplier_id)}) for c in cands]
    ctx = Context(
        req=req,
        now=now,
        product_ok=pok,
        product_note=pnote,
        destination_ok=dok,
        destination_note=dnote,
        reliability_min_sample=limits.min_reliability_sample,
    )
    return cands, ctx, warnings, truncated


def snapshot_of(cands: list[Candidate]) -> tuple[list[dict[str, Any]], str]:
    terms = [c.terms() for c in cands]
    return terms, hashlib.sha256(json.dumps(terms, sort_keys=True, default=str).encode()).hexdigest()
