"""Dashboard aggregation: reads persisted ATLAS records and returns registry-described metrics. No metric is stored or invented.

Rules: unknown is never zero (``no_data`` / ``unavailable``); filters are applied before aggregation and the response says which filters each
metric honoured; windows are UTC and half-open [start, end); changes against the previous window are reported only when both windows can be
computed and the previous value is not zero (otherwise no percentage); a structural reach or a configured value is never presented as a loss
or a measurement.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.analytics import registry as R
from app.models.decision import DecisionCase, DecisionEvent
from app.models.delivery import DeliveryAssessment
from app.models.enums import EdgeCriticality, EdgeStatus, EntityStatus, ScenarioStatus, SimStatus
from app.models.facility import Facility
from app.models.forecast import ForecastAlert
from app.models.inventory import InventoryPosition, StockoutAnalysis, StockoutFinding
from app.models.invopt import ReplenishmentRun
from app.models.product import Product
from app.models.registry import AuditEvent
from app.models.relationship import SupplyRelationship
from app.models.resilience import ResilienceRun
from app.models.risk import RiskAlert, RiskAssessment
from app.models.scenario import Scenario, SimulationRun
from app.models.sourcing import SourcingRun
from app.models.supplier import Supplier
from app.models.transport import TransportLane

MAX_ROWS = 5000
MAX_SUPPLIERS = 20000
SEVERITY_RANK = {"CRITICAL": 4, "ERROR": 3, "WARNING": 2, "INFO": 1}
RISK_CLASSES = ["LOW", "MODERATE", "HIGH", "CRITICAL"]
AT_RISK = ("current_shortage", "projected_stockout")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Filters:
    window_days: int = R.DEFAULT_WINDOW
    region: str | None = None
    category: str | None = None
    risk_class: str | None = None  # LOW | MODERATE | HIGH | CRITICAL | UNKNOWN

    def key(self) -> tuple:
        return (self.window_days, self.region, self.category, self.risk_class)

    def applied(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            **{
                k: v
                for k, v in (("region", self.region), ("category", self.category), ("risk_class", self.risk_class))
                if v
            },
        }


class TTLCache:
    """Short-lived, process-local cache. The key always carries the organisation, the role tier and every filter, so a response is never shared across
    incompatible scopes; entries are bounded in number and age."""

    def __init__(self, ttl: float = 5.0, max_items: int = 128, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl, self.max_items, self.clock = ttl, max_items, clock
        self._d: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any | None:
        with self._lock:
            hit = self._d.get(key)
            if hit and self.clock() - hit[0] <= self.ttl:
                return hit[1]
            self._d.pop(key, None)
            return None

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            if len(self._d) >= self.max_items:
                self._d.pop(next(iter(self._d)))
            self._d[key] = (self.clock(), value)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


def iso(d: datetime | None) -> str | None:
    if d is None:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def aware(d: datetime | None) -> datetime | None:
    return None if d is None else (d if d.tzinfo else d.replace(tzinfo=timezone.utc))


def window(now: datetime, days: int) -> tuple[datetime, datetime, datetime]:
    start = now - timedelta(days=days)
    return start, now, start - timedelta(days=days)


def metric(mid: str, value: Any, *, state: str = "ok", reason: str | None = None, as_of: datetime | None = None, cmp: dict[str, Any] | None = None,
           breakdown: dict[str, Any] | None = None, f: Filters | None = None, warnings: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
    d = R.BY_ID[mid]
    honoured = [x for x in d["filters"] if f and getattr(f, x if x != "risk_class" else "risk_class") is not None]
    return {"id": mid, "label": d["label"], "area": d["area"], "unit": d["unit"], "value": value, "state": state, "reason": reason, "as_of": iso(as_of), "direction": d["direction"],
            "time_basis": d["time_basis"], "comparison": cmp, "breakdown": breakdown, "filters_honoured": honoured, "drilldown": d["drilldown"], "warnings": warnings or []}  # fmt: skip


def compare(mid: str, cur: float, prev: float) -> dict[str, Any]:
    """Change against the previous equal window. No percentage when the previous value is 0; neutral metrics are 'changed', never good or bad."""
    diff = cur - prev
    direction = R.BY_ID[mid]["direction"]
    pct = None if prev == 0 else 100.0 * diff / abs(prev)
    if diff == 0:
        verdict = "same"
    elif direction == "neutral":
        verdict = "changed"
    else:
        verdict = "better" if (diff < 0) == (direction == "min") else "worse"
    return {
        "previous": prev,
        "change": diff,
        "change_pct": pct,
        "verdict": verdict,
        "note": "previous value is 0, so a percentage is undefined" if prev == 0 and diff != 0 else None,
    }


def windowed(
    db: Session, mid: str, f: Filters, now: datetime, q: Callable[[datetime, datetime], int]
) -> dict[str, Any]:
    s, e, ps = window(now, f.window_days)
    cur, prev = q(s, e), q(ps, s)
    return metric(mid, cur, cmp=compare(mid, cur, prev), f=f, as_of=now)


# ---- suppliers under the filters --------------------------------------------------------------------------------------------------------
def current_classes(db: Session, ids: list[int]) -> dict[int, tuple[str | None, RiskAssessment]]:
    out: dict[int, tuple[str | None, RiskAssessment]] = {}
    for i in range(0, len(ids), 500):
        for a in db.scalars(
            select(RiskAssessment).where(
                RiskAssessment.is_current.is_(True), RiskAssessment.supplier_id.in_(ids[i : i + 500])
            )
        ):
            out[a.supplier_id] = (a.classification.value if a.classification else None, a)
    return out


def supplier_scope(
    db: Session, f: Filters
) -> tuple[list[Supplier], dict[int, tuple[str | None, RiskAssessment]], bool]:
    st = select(Supplier).where(Supplier.status == EntityStatus.ACTIVE, Supplier.merged_into_id.is_(None))
    if f.region:
        st = st.where(Supplier.country_or_region == f.region)
    if f.category:
        st = st.where(Supplier.category == f.category)
    rows = list(db.scalars(st.order_by(Supplier.id).limit(MAX_SUPPLIERS + 1)))
    truncated = len(rows) > MAX_SUPPLIERS
    rows = rows[:MAX_SUPPLIERS]
    cls = current_classes(db, [s.id for s in rows])
    if f.risk_class:
        want = None if f.risk_class == "UNKNOWN" else f.risk_class
        rows = [s for s in rows if (cls.get(s.id, (None, None))[0]) == want]
    return rows, cls, truncated


def count(db: Session, st) -> int:  # noqa: ANN001
    return int(db.scalar(select(func.count()).select_from(st.subquery())) or 0)


# ---- areas ------------------------------------------------------------------------------------------------------------------------------
def network(db: Session, f: Filters, now: datetime) -> list[dict[str, Any]]:
    sup, _, trunc = supplier_scope(db, f)
    fac = select(Facility.id).where(Facility.status == EntityStatus.ACTIVE)
    if f.region:
        fac = fac.where(Facility.country_or_region == f.region)
    rel_ok = SupplyRelationship.status == EdgeStatus.CONFIRMED
    crit = (
        db.scalar(
            select(func.count())
            .select_from(SupplyRelationship)
            .where(rel_ok, SupplyRelationship.criticality == EdgeCriticality.CRITICAL)
        )
        or 0
    )
    per_fac = db.execute(
        select(SupplyRelationship.target_facility_id, func.count(func.distinct(SupplyRelationship.supplier_id)))
        .where(rel_ok, SupplyRelationship.supplier_id.is_not(None))
        .group_by(SupplyRelationship.target_facility_id)
    ).all()
    return [
        metric(
            "NET_SUPPLIERS_ACTIVE",
            len(sup),
            f=f,
            as_of=now,
            warnings=[f"Only the first {MAX_SUPPLIERS} suppliers were read."] if trunc else None,
        ),
        metric("NET_FACILITIES_ACTIVE", count(db, fac), f=f, as_of=now),
        metric("NET_PRODUCTS", int(db.scalar(select(func.count()).select_from(Product)) or 0), f=f, as_of=now),
        metric(
            "NET_RELATIONSHIPS_CONFIRMED",
            int(db.scalar(select(func.count()).select_from(SupplyRelationship).where(rel_ok)) or 0),
            f=f,
            as_of=now,
        ),
        metric("NET_CRITICAL_RELATIONSHIPS", int(crit), f=f, as_of=now),
        metric(
            "NET_SINGLE_SOURCE_FACILITIES",
            sum(1 for _, n in per_fac if n == 1),
            f=f,
            as_of=now,
            warnings=None if per_fac else ["No confirmed supplier relationship exists."],
        ),
    ]


def supplier_risk(db: Session, f: Filters, now: datetime) -> list[dict[str, Any]]:
    sup, cls, trunc = supplier_scope(db, f)
    dist = Counter((cls.get(s.id, (None, None))[0] or "UNKNOWN") for s in sup)
    classified = sum(v for k, v in dist.items() if k != "UNKNOWN")
    pending = sum(
        1 for s in sup if s.id in cls and cls[s.id][1].requires_review and cls[s.id][1].review_state == "UNREVIEWED"
    )
    latest = max((aware(cls[s.id][1].computed_at) for s in sup if s.id in cls), default=None)
    w = [f"Only the first {MAX_SUPPLIERS} suppliers were read."] if trunc else None
    return [
        metric(
            "RISK_ASSESSED_SUPPLIERS",
            classified,
            f=f,
            as_of=latest,
            breakdown={k: dist.get(k, 0) for k in RISK_CLASSES + ["UNKNOWN"]},
            warnings=w,
        ),
        metric("RISK_UNASSESSED_SUPPLIERS", dist.get("UNKNOWN", 0), f=f, as_of=now, warnings=w),
        metric(
            "RISK_HIGH_OR_CRITICAL",
            dist.get("HIGH", 0) + dist.get("CRITICAL", 0),
            f=f,
            as_of=latest,
            state="ok" if classified else "no_data",
            reason=None if classified else "No supplier has a risk classification yet; this is unknown, not zero.",
            warnings=w,
        ),
        metric("RISK_REVIEW_PENDING", pending, f=f, as_of=now),
    ]


def risk_alert_query(f: Filters):  # noqa: ANN201
    st = select(RiskAlert).join(Supplier, Supplier.id == RiskAlert.supplier_id)
    if f.region:
        st = st.where(Supplier.country_or_region == f.region)
    if f.category:
        st = st.where(Supplier.category == f.category)
    return st


def inventory(db: Session, f: Filters, now: datetime) -> list[dict[str, Any]]:
    an = db.scalar(
        select(StockoutAnalysis)
        .where(
            StockoutAnalysis.kind == "BASELINE", StockoutAnalysis.status.in_((SimStatus.COMPLETED, SimStatus.PARTIAL))
        )
        .order_by(StockoutAnalysis.id.desc())
        .limit(1)
    )
    out: list[dict[str, Any]] = []
    if an is None:
        for mid in ("INV_PAIRS_ANALYSED", "INV_AT_RISK_PAIRS", "INV_LOW_COVERAGE_PAIRS", "INV_UNKNOWN_PAIRS"):
            out.append(
                metric(
                    mid,
                    None,
                    state="no_data",
                    reason="No completed baseline stockout analysis exists; run one in Inventory & stockout.",
                    f=f,
                )
            )
    else:
        by = dict(
            db.execute(
                select(StockoutFinding.status, func.count())
                .where(StockoutFinding.analysis_id == an.id)
                .group_by(StockoutFinding.status)
            ).all()
        )
        total = sum(by.values())
        w = ["The analysis was truncated by a server bound; counts are a lower bound."] if an.truncated else []
        a = aware(an.finished_at) or aware(an.as_of)
        out += [
            metric("INV_PAIRS_ANALYSED", total, as_of=a, breakdown=by, f=f, warnings=w),
            metric(
                "INV_AT_RISK_PAIRS",
                sum(by.get(k, 0) for k in AT_RISK),
                as_of=a,
                f=f,
                warnings=w,
                breakdown={k: by.get(k, 0) for k in AT_RISK},
            ),
            metric("INV_LOW_COVERAGE_PAIRS", by.get("low_coverage", 0), as_of=a, f=f, warnings=w),
            metric(
                "INV_UNKNOWN_PAIRS", by.get("unknown", 0) + by.get("insufficient_data", 0), as_of=a, f=f, warnings=w
            ),
        ]
    last = db.scalar(select(func.max(InventoryPosition.observed_at)))
    out.append(
        metric(
            "INV_POSITION_FRESHNESS",
            iso(aware(last)),
            state="ok" if last else "no_data",
            reason=None if last else "No inventory positions have been imported.",
            as_of=aware(last),
            f=f,
        )
    )
    return out


def logistics(db: Session, f: Filters, now: datetime) -> list[dict[str, Any]]:
    def n(status: str):  # noqa: ANN202
        return lambda s, e: int(
            db.scalar(
                select(func.count())
                .select_from(DeliveryAssessment)
                .where(DeliveryAssessment.status == status, DeliveryAssessment.as_of >= s, DeliveryAssessment.as_of < e)
            )
            or 0
        )

    leads = [
        float(x)
        for x in db.scalars(
            select(SupplyRelationship.lead_time_days)
            .where(SupplyRelationship.status == EdgeStatus.CONFIRMED, SupplyRelationship.lead_time_days.is_not(None))
            .limit(MAX_ROWS)
        )
    ]
    return [
        windowed(db, "LOG_DELIVERIES_ASSESSED", f, now, n("ASSESSED")),
        windowed(db, "LOG_DELIVERIES_UNAVAILABLE", f, now, n("UNAVAILABLE")),
        metric(
            "LOG_CONFIGURED_LEAD_TIME_MEDIAN",
            round(statistics.median(leads), 2) if leads else None,
            state="ok" if leads else "no_data",
            reason=None if leads else "No confirmed relationship states a lead time.",
            f=f,
            as_of=now,
            warnings=["Configured values, not measured deliveries."] if leads else None,
        ),
        metric("LOG_LANES", int(db.scalar(select(func.count()).select_from(TransportLane)) or 0), f=f, as_of=now),
    ]


def scenarios(db: Session, f: Filters, now: datetime) -> list[dict[str, Any]]:
    fin = (SimStatus.COMPLETED, SimStatus.PARTIAL)

    def runs(status: tuple[SimStatus, ...]):  # noqa: ANN202
        return lambda s, e: int(
            db.scalar(
                select(func.count())
                .select_from(SimulationRun)
                .where(SimulationRun.status.in_(status), SimulationRun.queued_at >= s, SimulationRun.queued_at < e)
            )
            or 0
        )

    s, e, _ = window(now, f.window_days)
    top = db.execute(
        select(SimulationRun.entities_affected, SimulationRun.run_uid, SimulationRun.finished_at)
        .where(SimulationRun.status.in_(fin), SimulationRun.queued_at >= s, SimulationRun.queued_at < e)
        .order_by(SimulationRun.entities_affected.desc())
        .limit(1)
    ).first()
    return [
        metric(
            "SCN_SCENARIOS_READY",
            int(
                db.scalar(select(func.count()).select_from(Scenario).where(Scenario.status == ScenarioStatus.READY))
                or 0
            ),
            f=f,
            as_of=now,
        ),
        windowed(db, "SCN_RUNS", f, now, runs(fin)),
        windowed(db, "SCN_RUNS_FAILED", f, now, runs((SimStatus.FAILED,))),
        metric(
            "SCN_MAX_REACH",
            top[0] if top else None,
            state="ok" if top else "no_data",
            reason=None if top else "No finished scenario run in the window.",
            as_of=aware(top[2]) if top else None,
            f=f,
            breakdown={"run_uid": top[1]} if top else None,
        ),
    ]


def _runs_in(db: Session, model, s: datetime, e: datetime, *conds) -> int:  # noqa: ANN001
    return int(
        db.scalar(select(func.count()).select_from(model).where(model.queued_at >= s, model.queued_at < e, *conds)) or 0
    )


def optimization(db: Session, f: Filters, now: datetime) -> list[dict[str, Any]]:
    def done(model):  # noqa: ANN001, ANN202
        return lambda s, e: _runs_in(db, model, s, e, model.status == SimStatus.COMPLETED)

    bad_status = ("infeasible", "timed_out", "insufficient_data", "invalid_input", "failed")

    def unsuccessful(s: datetime, e: datetime) -> int:
        t = 0
        for m in (ResilienceRun, SourcingRun, ReplenishmentRun):
            t += _runs_in(
                db,
                m,
                s,
                e,
                or_(
                    m.status == SimStatus.FAILED, and_(m.status == SimStatus.COMPLETED, m.result_status.in_(bad_status))
                ),
            )
        return t

    return [
        windowed(db, "OPT_RESILIENCE_RUNS", f, now, done(ResilienceRun)),
        windowed(db, "OPT_SOURCING_RUNS", f, now, done(SourcingRun)),
        windowed(db, "OPT_REPLENISHMENT_RUNS", f, now, done(ReplenishmentRun)),
        windowed(db, "OPT_UNSUCCESSFUL_RUNS", f, now, unsuccessful),
    ]


def operations(db: Session, f: Filters, now: datetime) -> list[dict[str, Any]]:
    alerts = count(db, risk_alert_query(f).where(RiskAlert.status == "OPEN"))
    by_sev = dict(
        db.execute(
            select(RiskAlert.severity, func.count())
            .join(Supplier, Supplier.id == RiskAlert.supplier_id)
            .where(
                RiskAlert.status == "OPEN",
                *([Supplier.country_or_region == f.region] if f.region else []),
                *([Supplier.category == f.category] if f.category else []),
            )
            .group_by(RiskAlert.severity)
        ).all()
    )
    dc = lambda st: int(db.scalar(select(func.count()).select_from(DecisionCase).where(DecisionCase.status == st)) or 0)  # noqa: E731
    fa = lambda s, e: int(
        db.scalar(
            select(func.count())
            .select_from(ForecastAlert)
            .where(ForecastAlert.created_at >= s, ForecastAlert.created_at < e)
        )
        or 0
    )  # noqa: E731
    return [
        metric(
            "RISK_ALERTS_OPEN",
            alerts,
            f=f,
            as_of=now,
            breakdown={getattr(k, "value", str(k)): v for k, v in by_sev.items()},
        ),
        metric("OPS_DECISIONS_UNDER_REVIEW", dc("UNDER_REVIEW"), f=f, as_of=now),
        metric("OPS_DECISIONS_AWAITING_SUBMISSION", dc("EVALUATED"), f=f, as_of=now),
        windowed(db, "OPS_FORECAST_ALERTS", f, now, fa),
    ]


AREA_FN = {
    "network": network,
    "supplier_risk": supplier_risk,
    "inventory": inventory,
    "logistics": logistics,
    "scenarios": scenarios,
    "optimization": optimization,
    "operations": operations,
}


def area_metrics(db: Session, area: str, f: Filters, now: datetime) -> list[dict[str, Any]]:
    return AREA_FN[area](db, f, now)


def all_metrics(db: Session, f: Filters, now: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in R.AREAS:
        out += area_metrics(db, a, f, now)
    # supplier-risk alerts belong to operations in the registry; avoid duplicates if an area returned the same id twice
    seen: set[str] = set()
    return [m for m in out if not (m["id"] in seen or seen.add(m["id"]))]


def ignored_filters(area: str, f: Filters) -> list[str]:
    ds = [m for m in R.REGISTRY if m["area"] == area]
    honoured = {x for m in ds for x in m["filters"]}
    return [k for k in ("region", "category", "risk_class") if getattr(f, k) is not None and k not in honoured]


def meta(f: Filters, now: datetime, area: str | None = None, cached: bool = False) -> dict[str, Any]:
    s, e, ps = window(now, f.window_days)
    return {"registry_version": R.REGISTRY_VERSION, "generated_at": iso(now), "timezone": "UTC", "window": {"days": f.window_days, "start": iso(s), "end": iso(e), "previous_start": iso(ps)},
            "applied_filters": f.applied(), "ignored_filters": ignored_filters(area, f) if area else [], "cached": cached, "scope": "the caller's organisation, as visible to the role"}  # fmt: skip


# ---- detail views -----------------------------------------------------------------------------------------------------------------------
def risk_table(db: Session, f: Filters, limit: int, offset: int) -> dict[str, Any]:
    sup, cls, trunc = supplier_scope(db, f)
    rows = []
    for s in sup:
        c, a = cls.get(s.id, (None, None))
        rows.append(
            (RISK_CLASSES.index(c) if c else -1, a.risk_score if a and a.risk_score is not None else -1.0, s, c, a)
        )
    rows.sort(key=lambda r: (-r[0], -r[1], r[2].supplier_code))
    items = [{"supplier_id": s.id, "supplier_code": s.supplier_code, "name": s.name, "region": s.country_or_region, "category": s.category, "classification": c or "UNKNOWN",
              "risk_score": a.risk_score if a else None, "evidence_coverage": a.evidence_coverage if a else None, "status": a.status.value if a else None, "basis": f"rule-based methodology {a.methodology_version}" if a else "no assessment",
              "evidence_latest_at": iso(aware(a.evidence_latest_at)) if a else None, "computed_at": iso(aware(a.computed_at)) if a else None, "requires_review": bool(a.requires_review) if a else False,
              "kind": "heuristic score from configured rules; not a prediction of an incident"} for _, _, s, c, a in rows[offset : offset + limit]]  # fmt: skip
    return {"items": items, "total": len(rows), "limit": limit, "offset": offset, "truncated": trunc}


def supplier_breakdowns(db: Session, f: Filters) -> dict[str, Any]:
    sup, cls, _ = supplier_scope(db, f)
    by_region: dict[str, Counter] = defaultdict(Counter)
    by_cat: dict[str, Counter] = defaultdict(Counter)
    for s in sup:
        c = cls.get(s.id, (None, None))[0] or "UNKNOWN"
        by_region[s.country_or_region][c] += 1
        by_cat[s.category][c] += 1
    shape = lambda d: sorted(
        (
            {"key": k, "total": sum(v.values()), **{c: v.get(c, 0) for c in RISK_CLASSES + ["UNKNOWN"]}}
            for k, v in d.items()
        ),
        key=lambda r: -r["total"],
    )[:25]  # noqa: E731
    ids = {s.id for s in sup if (cls.get(s.id, (None, None))[0]) in ("HIGH", "CRITICAL")}
    in_scope = {s.id for s in sup}
    cap: dict[str, dict[str, float]] = defaultdict(lambda: {"all": 0.0, "high_or_critical": 0.0})
    for sid, v, u in db.execute(
        select(SupplyRelationship.supplier_id, SupplyRelationship.capacity_value, SupplyRelationship.capacity_unit)
        .where(
            SupplyRelationship.status == EdgeStatus.CONFIRMED,
            SupplyRelationship.capacity_value.is_not(None),
            SupplyRelationship.capacity_unit.is_not(None),
        )
        .limit(MAX_ROWS)
    ):
        if sid in in_scope:
            cap[u]["all"] += v
            if sid in ids:
                cap[u]["high_or_critical"] += v
    return {"by_region": shape(by_region), "by_category": shape(by_cat),
            "capacity_exposure": [{"unit": u, "all_confirmed_capacity": round(v["all"], 3), "high_or_critical_capacity": round(v["high_or_critical"], 3), "share": None if v["all"] == 0 else round(v["high_or_critical"] / v["all"], 4)} for u, v in sorted(cap.items())],
            "capacity_note": "Capacity is summed per unit only; different units are never added. It is capacity recorded on confirmed relationships, not realised volume."}  # fmt: skip


def inventory_table(db: Session, limit: int, offset: int, status: str | None) -> dict[str, Any]:
    an = db.scalar(
        select(StockoutAnalysis)
        .where(
            StockoutAnalysis.kind == "BASELINE", StockoutAnalysis.status.in_((SimStatus.COMPLETED, SimStatus.PARTIAL))
        )
        .order_by(StockoutAnalysis.id.desc())
        .limit(1)
    )
    if an is None:
        return {"analysis": None, "items": [], "total": 0, "limit": limit, "offset": offset, "by_status": {}}
    st = select(StockoutFinding).where(StockoutFinding.analysis_id == an.id)
    st = (
        st.where(StockoutFinding.status == status)
        if status
        else st.where(StockoutFinding.status.in_(AT_RISK + ("low_coverage",)))
    )
    total = count(db, st)
    rows = db.scalars(st.order_by(StockoutFinding.severity_rank.desc(), StockoutFinding.id).limit(limit).offset(offset))
    by = dict(
        db.execute(
            select(StockoutFinding.status, func.count())
            .where(StockoutFinding.analysis_id == an.id)
            .group_by(StockoutFinding.status)
        ).all()
    )
    return {"analysis": {"analysis_uid": an.analysis_uid, "as_of": iso(aware(an.as_of)), "finished_at": iso(aware(an.finished_at)), "algorithm_version": an.algorithm_version, "truncated": bool(an.truncated)},
            "items": [{"product_code": r.product_code, "product_name": r.product_name, "facility_code": r.facility_code, "facility_name": r.facility_name, "unit": r.unit, "status": r.status, "reason": r.reason, "confidence": r.confidence, "input_coverage": r.coverage} for r in rows],
            "total": total, "limit": limit, "offset": offset, "by_status": by,
            "note": "Positions are read from the stockout analysis, which separates on-hand, reserved, in-transit and backordered quantities by the inventory definitions; the dashboard does not recombine them."}  # fmt: skip


def lead_time_histogram(db: Session) -> dict[str, Any]:
    leads = [
        float(x)
        for x in db.scalars(
            select(SupplyRelationship.lead_time_days)
            .where(SupplyRelationship.status == EdgeStatus.CONFIRMED, SupplyRelationship.lead_time_days.is_not(None))
            .limit(MAX_ROWS)
        )
    ]
    edges = [(0, 7), (7, 14), (14, 30), (30, 60), (60, 10**9)]
    labels = ["under 7 days", "7 to 14 days", "14 to 30 days", "30 to 60 days", "60 days or more"]
    return {
        "basis": "CONFIGURED lead times on confirmed relationships (not measured deliveries)",
        "n": len(leads),
        "bins": [{"label": lb, "count": sum(1 for x in leads if lo <= x < hi)} for lb, (lo, hi) in zip(labels, edges)],
    }


def single_source(db: Session, limit: int, offset: int) -> dict[str, Any]:
    rows = db.execute(
        select(
            SupplyRelationship.target_facility_id,
            SupplyRelationship.supplier_id,
            SupplyRelationship.criticality,
            SupplyRelationship.supply_share_pct,
        ).where(SupplyRelationship.status == EdgeStatus.CONFIRMED, SupplyRelationship.supplier_id.is_not(None))
    ).all()
    by: dict[int, list] = defaultdict(list)
    for fid, sid, crit, share in rows:
        by[fid].append((sid, crit, share))
    singles = sorted(fid for fid, v in by.items() if len({x[0] for x in v}) == 1)
    page = singles[offset : offset + limit]
    facs = {x.id: x for x in db.scalars(select(Facility).where(Facility.id.in_(page)))} if page else {}
    sups = (
        {x.id: x for x in db.scalars(select(Supplier).where(Supplier.id.in_([by[i][0][0] for i in page])))}
        if page
        else {}
    )
    items = []
    for fid in page:
        sid, crit, share = by[fid][0]
        fa, su = facs.get(fid), sups.get(sid)
        items.append(
            {
                "facility_code": fa.facility_code if fa else None,
                "facility_name": fa.name if fa else None,
                "supplier_code": su.supplier_code if su else None,
                "supplier_name": su.name if su else None,
                "criticality": crit.value if crit else None,
                "supply_share_pct": share,
            }
        )
    return {
        "items": items,
        "total": len(singles),
        "limit": limit,
        "offset": offset,
        "note": "Structural dependency only: one confirmed supplier serves the facility. It says nothing about volume at risk.",
    }


def criticality_distribution(db: Session) -> dict[str, int]:
    d = dict(
        db.execute(
            select(SupplyRelationship.criticality, func.count())
            .where(SupplyRelationship.status == EdgeStatus.CONFIRMED)
            .group_by(SupplyRelationship.criticality)
        ).all()
    )
    return {(k.value if k else "UNRATED"): v for k, v in d.items()}


def recent_runs(db: Session, limit: int, offset: int, kinds: set[str] | None = None) -> list[dict[str, Any]]:
    n = limit + offset
    out: list[dict[str, Any]] = []

    def add(kind: str, rows, link: str, title: Callable[[Any], str]) -> None:  # noqa: ANN001
        if kinds and kind not in kinds:
            return
        for r in rows:
            out.append(
                {
                    "kind": kind,
                    "uid": r.run_uid,
                    "title": title(r),
                    "status": r.status.value,
                    "result_status": getattr(r, "result_status", None),
                    "at": iso(aware(r.finished_at or r.queued_at)),
                    "actor": getattr(r, "requested_by", None),
                    "link": f"{link}/{r.run_uid}",
                }
            )

    add(
        "simulation",
        db.scalars(select(SimulationRun).order_by(SimulationRun.id.desc()).limit(n)),
        "/scenarios/runs",
        lambda r: f"Scenario run (scenario {r.scenario_id} v{r.scenario_version})",
    )
    add(
        "resilience",
        db.scalars(select(ResilienceRun).order_by(ResilienceRun.id.desc()).limit(n)),
        "/resilience/runs",
        lambda r: f"Resilience optimisation ({r.strategy.lower().replace('_', ' ')})",
    )
    add(
        "sourcing",
        db.scalars(select(SourcingRun).order_by(SourcingRun.id.desc()).limit(n)),
        "/sourcing/runs",
        lambda r: f"Sourcing {r.kind.lower()}",
    )
    add(
        "replenishment",
        db.scalars(select(ReplenishmentRun).order_by(ReplenishmentRun.id.desc()).limit(n)),
        "/replenishment/runs",
        lambda r: "Replenishment run",
    )
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    return out


def activity(db: Session, limit: int, offset: int, include_audit: bool, audit_where: Any = None) -> dict[str, Any]:
    n = limit + offset
    items = recent_runs(db, limit, offset)
    for e in db.scalars(
        select(DecisionEvent)
        .where(DecisionEvent.kind.in_(("SUBMITTED", "APPROVED", "REJECTED", "RETURNED", "SUPERSEDED", "CREATED")))
        .order_by(DecisionEvent.id.desc())
        .limit(n)
    ):
        case = db.get(DecisionCase, e.case_id)
        items.append({"kind": "decision", "uid": case.case_uid if case else str(e.case_id), "title": f"Decision case {e.kind.lower()}" + (f": {case.title}" if case else ""), "status": e.to_status, "result_status": None, "at": iso(aware(e.at)), "actor": e.actor, "link": f"/decision/cases/{case.case_uid}" if case else "/decision"})  # fmt: skip
    if include_audit:
        aq = select(AuditEvent) if audit_where is None else select(AuditEvent).where(audit_where)
        for a in db.scalars(aq.order_by(AuditEvent.id.desc()).limit(n)):
            items.append(
                {
                    "kind": "audit",
                    "uid": str(a.id),
                    "title": a.action,
                    "status": a.outcome.value if hasattr(a.outcome, "value") else str(a.outcome),
                    "result_status": None,
                    "at": iso(aware(a.occurred_at)),
                    "actor": a.actor,
                    "link": None,
                }
            )
    items.sort(key=lambda x: x["at"] or "", reverse=True)
    return {"items": items[offset : offset + limit], "limit": limit, "offset": offset, "audit_included": include_audit,
            "note": "Newest first across run, decision and (for reviewers and above) audit records. Counts are not totals: each source is read to a bounded depth."}  # fmt: skip


def alerts(db: Session, f: Filters, limit: int, offset: int) -> dict[str, Any]:
    n = limit + offset
    out: list[dict[str, Any]] = []
    for a in db.scalars(
        risk_alert_query(f)
        .where(RiskAlert.status.in_(("OPEN", "ACKNOWLEDGED")))
        .order_by(RiskAlert.id.desc())
        .limit(max(n, 1) * 3)
    ):
        out.append(
            {
                "source": "supplier_risk",
                "id": a.id,
                "severity": a.severity.value,
                "status": a.status.value,
                "reason": a.reason,
                "supplier_id": a.supplier_id,
                "at": iso(aware(a.last_seen_at)),
                "link": "/risk",
                "occurrences": a.occurrences,
            }
        )
    s, e, _ = window(utcnow(), f.window_days)
    for a in db.scalars(
        select(ForecastAlert).where(ForecastAlert.created_at >= s).order_by(ForecastAlert.id.desc()).limit(n)
    ):
        out.append(
            {
                "source": "forecast",
                "id": a.id,
                "severity": a.severity,
                "status": "RAISED",
                "reason": a.message,
                "supplier_id": None,
                "at": iso(aware(a.created_at)),
                "link": "/forecast",
                "occurrences": 1,
            }
        )
    out.sort(key=lambda x: (-SEVERITY_RANK.get(x["severity"], 0), x["at"] or ""), reverse=False)
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    out.sort(key=lambda x: -SEVERITY_RANK.get(x["severity"], 0))
    return {"items": out[offset : offset + limit], "total": len(out), "limit": limit, "offset": offset, "priority": "Severity (critical, error, warning, info), then newest first. Only persisted alerts are listed; there is no alert engine beyond supplier-risk and forecast alerts."}  # fmt: skip


def risk_trend(db: Session, f: Filters, now: datetime) -> dict[str, Any]:
    sup, _, _ = supplier_scope(db, f)
    ids = [s.id for s in sup]
    hist: dict[int, list[tuple[datetime, str | None]]] = defaultdict(list)
    for i in range(0, len(ids), 500):
        for sid, at, c in db.execute(
            select(RiskAssessment.supplier_id, RiskAssessment.computed_at, RiskAssessment.classification)
            .where(RiskAssessment.supplier_id.in_(ids[i : i + 500]))
            .order_by(RiskAssessment.computed_at)
            .limit(MAX_ROWS * 10)
        ):
            hist[sid].append((aware(at), c.value if c else None))
    pts = []
    for k in range(f.window_days, -1, -1):
        day_end = (now - timedelta(days=k)).replace(hour=23, minute=59, second=59, microsecond=0)
        if day_end > now:
            day_end = now
        classified = high = 0
        for sid in ids:
            last = None
            for at, c in hist.get(sid, ()):
                if at <= day_end:
                    last = c
                else:
                    break
            if last:
                classified += 1
                high += last in ("HIGH", "CRITICAL")
        pts.append(
            {
                "date": day_end.date().isoformat(),
                "suppliers_in_scope": len(ids),
                "classified": classified,
                "high_or_critical": high,
                "unknown": len(ids) - classified,
            }
        )
    return {
        "points": pts,
        "basis": "Latest classified assessment of each in-scope supplier on or before the end of each UTC day. Suppliers with no assessment yet are 'unknown', not low. Reconstructed from stored assessments, so a value before the first assessment is unknown.",
    }


def activity_trend(db: Session, now: datetime, days: int) -> dict[str, Any]:
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    series: dict[str, Counter] = {}
    for key, model in (
        ("scenario_runs", SimulationRun),
        ("resilience_runs", ResilienceRun),
        ("sourcing_runs", SourcingRun),
        ("replenishment_runs", ReplenishmentRun),
    ):
        c: Counter = Counter()
        for (at,) in db.execute(select(model.queued_at).where(model.queued_at >= start).limit(MAX_ROWS * 4)):
            c[aware(at).date().isoformat()] += 1
        series[key] = c
    dates = [(start + timedelta(days=i)).date().isoformat() for i in range(days + 1)]
    return {
        "dates": dates,
        "series": {k: [c.get(d, 0) for d in dates] for k, c in series.items()},
        "basis": "Runs queued per UTC day. This is analysis activity, not supply-chain performance.",
    }
