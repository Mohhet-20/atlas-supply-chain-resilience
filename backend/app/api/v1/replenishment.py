"""Inventory Optimization & Smart Replenishment API.

Calculates inventory position, safety stock and reorder policy for a product at a facility, optimises a replenishment policy against the
data registered in ATLAS, compares it with the current policy by simulation or backtest, evaluates disruption scenarios on copies, and keeps
every run as an immutable, reproducible record. A recommendation is decision support: ATLAS never creates a purchase order, and an approval
is an internal record. Data is limited to the caller's organisation; bodies reject unknown fields (including any organisation id) and within it, data is visible to
every user at or above the endpoint role. Costs (unit, ordering, holding, shortage) and every figure derived from them are reserved for editors.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select

from app.api.v1.deps import DbSession
from app.core.auth import Actor, Role, require
from app.core.errors import ConflictError, DomainValidationError, ForbiddenError, NotFoundError, WriteDisabledError
from app.db.tenancy import request_session_factory
from app.inventory import analysis as inv
from app.inventory import calc
from app.inventory import config as invcfg
from app.invopt import config as C
from app.invopt import data as D
from app.invopt import engine as E
from app.invopt import runs as R
from app.invopt.config import ReplenishRequest
from app.models.enums import Provenance, SimStatus
from app.models.facility import Facility
from app.models.inventory import InventoryPosition
from app.models.invopt import InventoryCostProfile, ReplenishmentDecision, ReplenishmentRun
from app.models.product import Product
from app.models.types import utcnow
from app.registry import audit
from app.registry.audit import request_id_of
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/replenishment", tags=["inventory optimization and smart replenishment"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
UidPath = Annotated[str, Path(min_length=4, max_length=40, pattern=r"^[A-Za-z0-9-]+$")]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Reviewer = Annotated[Actor, Depends(require(Role.REVIEWER))]
Lim = Annotated[int, Query(ge=1, le=200)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]
IdemKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
]
NOT_SUPPORTED = [
    "creating, sending or tracking purchase orders",
    "multi-echelon (network) inventory optimisation",
    "multi-product joint constraints, shared budgets or shared storage",
    "quantity discounts and price breaks",
    "shelf life, expiry and minimum remaining shelf life",
    "unit and currency conversion",
    "expediting, transfers and alternative-supplier switching inside the simulation (see Sourcing for alternatives)",
    "lost-sales cost beyond a stated shortage cost per unit",
    "automatic approval or execution",
]


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVED", "REJECTED"]
    rationale: str = Field(min_length=10, max_length=1000)


class CostProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(ge=1, le=2**31 - 1)
    facility_id: int = Field(ge=1, le=2**31 - 1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    holding_cost_per_unit_day: float | None = Field(default=None, ge=0, le=1e9)
    ordering_cost: float | None = Field(default=None, ge=0, le=1e12)
    shortage_cost_per_unit: float | None = Field(default=None, ge=0, le=1e12)
    unit_cost: float | None = Field(default=None, ge=0, le=1e12)
    storage_capacity: float | None = Field(default=None, gt=0, le=1e12)
    unit: str | None = Field(default=None, min_length=1, max_length=32)
    provenance: Literal["SYNTHETIC_DEMO", "USER_PROVIDED", "IMPORTED_EXTERNAL", "MODEL_ESTIMATE", "SIMULATED"] = (
        "USER_PROVIDED"
    )
    source_note: str | None = Field(default=None, max_length=300)
    effective_from: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> CostProfileIn:
        if all(
            v is None
            for v in (
                self.holding_cost_per_unit_day,
                self.ordering_cost,
                self.shortage_cost_per_unit,
                self.unit_cost,
                self.storage_capacity,
            )
        ):
            raise ValueError("Give at least one cost or the storage capacity; an empty profile carries no information.")
        if self.effective_from is not None and self.effective_from.tzinfo is None:
            raise ValueError("effective_from must include a timezone offset.")
        return self


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError(
            "Replenishment write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED)."
        )


def _limits(request: Request) -> C.Limits:
    s = request.app.state.settings
    return C.Limits(max_active_runs=s.invopt_max_active_runs, time_budget_seconds=s.invopt_time_budget_seconds, max_sim_steps=s.invopt_max_sim_steps, max_replications=s.invopt_max_replications, max_delivery_rows=s.invopt_max_delivery_rows, max_pairs_overview=s.invopt_max_pairs_overview)  # fmt: skip


def _iso(d: Any) -> str | None:
    return None if d is None else d.isoformat()


def _page(db, q, limit, offset, shape) -> dict[str, Any]:  # noqa: ANN001
    total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0
    return {
        "items": [shape(r) for r in db.scalars(q.limit(limit).offset(offset))],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _parse(model, body: dict[str, Any]):  # noqa: ANN001, ANN202
    try:
        return model.model_validate(body)
    except ValidationError as e:
        raise DomainValidationError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e


def _hide(actor: Actor, payload: Any) -> Any:
    if actor.role >= Role.EDITOR:
        return payload
    out = R.redact(payload)
    if isinstance(out, dict):
        out["cost_redacted"] = True
    return out


def _run_get(db, uid: str) -> ReplenishmentRun:  # noqa: ANN001
    r = db.scalar(select(ReplenishmentRun).where(ReplenishmentRun.run_uid == uid))
    if r is None:
        raise NotFoundError("Replenishment run not found.")
    return r


def _run(db, r: ReplenishmentRun, actor: Actor, full: bool = False) -> dict[str, Any]:  # noqa: ANN001
    rec = r.recommendation or {}
    req = r.request or {}
    out = {
        "run_uid": r.run_uid, "kind": r.kind, "status": r.status.value, "result_status": r.result_status, "requested_by": r.requested_by,
        "product_id": r.product_id, "facility_id": r.facility_id, "scenario_id": r.scenario_id, "forecast_run_id": r.forecast_run_id,
        "policy_type": req.get("policy_type"), "service_type": req.get("service_type"), "service_target": req.get("service_target"), "objective": req.get("objective"),
        "algorithm_version": r.algorithm_version, "snapshot_hash": r.snapshot_hash, "scenario_hash": r.scenario_hash, "recommendation_hash": r.recommendation_hash,
        "queued_at": _iso(r.queued_at), "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at), "duration_ms": r.duration_ms, "error": r.error,
        "order_now": rec.get("order_now"), "order_quantity": rec.get("quantity"), "order_status": rec.get("status"), "unit": rec.get("unit"),
        "safety_stock": (r.safety_stock or {}).get("value"), "hypothetical": bool((r.provenance or {}).get("hypothetical")),
        "approval": R.approval_state(db, r) if r.kind == "OPTIMIZE" else None,
    }  # fmt: skip
    if full:
        out.update({k: getattr(r, k) for k in ("request", "request_hash", "position", "demand", "lead_time", "safety_stock", "recommendation", "projection", "evaluation", "scenario_results", "mitigations", "infeasibility", "costs", "explanation", "warnings", "provenance", "reproducibility")})  # fmt: skip
        out["statements"] = [
            C.STATEMENTS[k]
            for k in (
                "decision_support",
                "service",
                "safety_stock",
                "simulation",
                "optimal",
                "cost",
                "missing",
                "tenancy",
            )
        ]
    return _hide(actor, out)


# ------------------------------------------------------------------------------------------------ meta
@router.get("/health", summary="Capabilities, limits and what is not supported")
def health(request: Request, db: DbSession, _a: Viewer) -> dict[str, Any]:
    s = request.app.state.settings
    runs = dict(db.execute(select(ReplenishmentRun.status, func.count()).group_by(ReplenishmentRun.status)).all())
    lim = _limits(request)
    return {
        "algorithm_version": C.ALGORITHM_VERSION, "write_api_enabled": s.write_api_allowed, "cost_profiles": db.scalar(select(func.count()).select_from(InventoryCostProfile)) or 0,
        "runs_by_status": {k.value: v for k, v in runs.items()},
        "limits": {"max_active_runs": lim.max_active_runs, "time_budget_seconds": lim.time_budget_seconds, "max_sim_steps": lim.max_sim_steps, "max_replications": lim.max_replications, "max_sim_days": lim.max_sim_days, "max_delivery_rows": lim.max_delivery_rows, "max_pairs_overview": lim.max_pairs_overview},  # fmt: skip
        "algorithms": ["inventory position = available - backordered + dated open receipts", "normal / normal with lead-time variability / bootstrap safety stock", "cycle service level and fill rate (normal loss function)", "(s, Q), (s, S) and (R, S) policies", "EOQ when its assumptions hold", "paired Monte Carlo over a candidate grid", "train/test backtest with train-only parameters", "disruption what-ifs on copies"],  # fmt: skip
        "not_supported": NOT_SUPPORTED, "statements": C.STATEMENTS,
    }  # fmt: skip


@router.get("/catalogue", summary="Policy types, safety-stock methods, service measures and objectives")
def catalogue(_a: Viewer) -> dict[str, Any]:
    return C.catalogue()


# ------------------------------------------------------------------------------------------------ cost profiles
def _profile_out(r: InventoryCostProfile, redact: bool) -> dict[str, Any]:
    d = {"profile_uid": r.profile_uid, "product_id": r.product_id, "facility_id": r.facility_id, "currency": r.currency, "holding_cost_per_unit_day": r.holding_cost_per_unit_day, "ordering_cost": r.ordering_cost, "shortage_cost_per_unit": r.shortage_cost_per_unit, "unit_cost": r.unit_cost, "storage_capacity": r.storage_capacity, "unit": r.unit, "provenance": r.provenance.value, "source_note": r.source_note, "effective_from": _iso(r.effective_from), "created_by": r.created_by, "created_at": _iso(r.created_at), "retired_at": _iso(r.retired_at)}  # fmt: skip
    if redact:
        for k in ("holding_cost_per_unit_day", "ordering_cost", "shortage_cost_per_unit", "unit_cost"):
            d[k] = None
        d["cost_redacted"] = True
    return d


@router.post(
    "/cost-profiles",
    status_code=201,
    responses=ERR,
    summary="Register inventory costs for a product at a facility (immutable; a change is a new profile)",
)
def create_profile(body: dict[str, Any], db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    b = _parse(CostProfileIn, body)
    with db.begin():
        p, f = db.get(Product, b.product_id), db.get(Facility, b.facility_id)
        if p is None:
            raise NotFoundError(f"Product {b.product_id} does not exist.")
        if f is None:
            raise NotFoundError(f"Facility {b.facility_id} does not exist.")
        if p.unit_of_measure and b.unit and p.unit_of_measure != b.unit:
            raise DomainValidationError(
                f"The product is measured in '{p.unit_of_measure}' but the profile is per '{b.unit}'. No unit conversion is applied."
            )
        row = InventoryCostProfile(
            profile_uid=f"CP-{uuid.uuid4().hex[:16].upper()}", product_id=b.product_id, facility_id=b.facility_id, currency=b.currency,
            holding_cost_per_unit_day=b.holding_cost_per_unit_day, ordering_cost=b.ordering_cost, shortage_cost_per_unit=b.shortage_cost_per_unit, unit_cost=b.unit_cost,
            storage_capacity=b.storage_capacity, unit=b.unit or p.unit_of_measure, provenance=Provenance(b.provenance), source_note=b.source_note,
            effective_from=b.effective_from or utcnow(), created_by=actor.name,
        )  # fmt: skip
        db.add(row)
        db.flush()
        audit.record(db, actor, "replenishment.cost_profile.create", target_type="INVENTORY_COST_PROFILE", target_id=row.id, details={"product": row.product_id, "facility": row.facility_id, "currency": row.currency}, request_id=request_id_of(request))  # fmt: skip
        return _profile_out(row, False)


@router.get("/cost-profiles", summary="Cost profiles (newest first). Cost figures are shown to editors and above only")
def list_profiles(db: DbSession, actor: Viewer, limit: Lim = 20, offset: Off = 0, product_id: Annotated[int | None, Query(ge=1)] = None, facility_id: Annotated[int | None, Query(ge=1)] = None, active: bool | None = None) -> dict[str, Any]:  # fmt: skip
    q = select(InventoryCostProfile)
    if product_id:
        q = q.where(InventoryCostProfile.product_id == product_id)
    if facility_id:
        q = q.where(InventoryCostProfile.facility_id == facility_id)
    if active is True:
        q = q.where(InventoryCostProfile.retired_at.is_(None))
    elif active is False:
        q = q.where(InventoryCostProfile.retired_at.is_not(None))
    return _page(
        db,
        q.order_by(InventoryCostProfile.id.desc()),
        limit,
        offset,
        lambda r: _profile_out(r, actor.role < Role.EDITOR),
    )


@router.get("/cost-profiles/{uid}", responses=ERR, summary="One cost profile")
def get_profile(uid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    r = db.scalar(select(InventoryCostProfile).where(InventoryCostProfile.profile_uid == uid))
    if r is None:
        raise NotFoundError("Cost profile not found.")
    return _profile_out(r, actor.role < Role.EDITOR)


@router.post(
    "/cost-profiles/{uid}/retire",
    responses=ERR,
    summary="Retire a cost profile (final; it stays visible for past runs)",
)
def retire_profile(uid: UidPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        r = db.scalar(select(InventoryCostProfile).where(InventoryCostProfile.profile_uid == uid))
        if r is None:
            raise NotFoundError("Cost profile not found.")
        if r.retired_at is not None:
            raise ConflictError("The cost profile is already retired.")
        r.retired_at = utcnow()
        audit.record(db, actor, "replenishment.cost_profile.retire", target_type="INVENTORY_COST_PROFILE", target_id=r.id, details={}, request_id=request_id_of(request))  # fmt: skip
        return _profile_out(r, False)


# ------------------------------------------------------------------------------------------------ positions overview
@router.get(
    "/positions", summary="Product-facility pairs with an inventory observation: cover, policy status and data gaps"
)
def positions(
    request: Request, db: DbSession, _a: Viewer, limit: Lim = 25, offset: Off = 0, product_id: Annotated[int | None, Query(ge=1)] = None, facility_id: Annotated[int | None, Query(ge=1)] = None,
    window_days: Annotated[int, Query(ge=14, le=365)] = 90, max_age_days: Annotated[int, Query(ge=1, le=365)] = 7,
) -> dict[str, Any]:  # fmt: skip
    lim = _limits(request)
    now = utcnow()
    q = select(InventoryPosition.product_id, InventoryPosition.facility_id).distinct()
    if product_id:
        q = q.where(InventoryPosition.product_id == product_id)
    if facility_id:
        q = q.where(InventoryPosition.facility_id == facility_id)
    pairs = sorted(db.execute(q).all())
    total = len(pairs)
    capped = total > lim.max_pairs_overview
    pairs = pairs[: lim.max_pairs_overview]
    page = pairs[offset : offset + limit]
    items: list[dict[str, Any]] = []
    gaps = {"no_demand": 0, "no_policy": 0, "stale": 0, "no_inventory_value": 0}
    if page:
        conf = invcfg.InventoryConfig(product_ids=sorted({p for p, _ in page}), facility_ids=sorted({f for _, f in page}), demand_window_days=window_days, max_inventory_age_days=max_age_days)  # fmt: skip
        keep = set(page)
        lp = {k: v for k, v in inv.latest_positions(db, conf, now).items() if k in keep}
        pol = {k: v for k, v in inv.effective_policies(db, conf, now).items() if k in keep}
        dem = {k: v for k, v in inv.window_demand(db, conf, now.date()).items() if k in keep}
        prods = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(conf.product_ids or [])))}
        facs = {f.id: f for f in db.scalars(select(Facility).where(Facility.id.in_(conf.facility_ids or [])))}
        for key in page:
            p, prod, fac = lp.get(key), prods.get(key[0]), facs.get(key[1])
            unit = (p.unit if p else None) or (prod.unit_of_measure if prod else None)
            row: dict[str, Any] = {"product_id": key[0], "facility_id": key[1], "product_code": prod.product_code if prod else None, "product_name": prod.name if prod else None, "facility_code": fac.facility_code if fac else None, "facility_name": fac.name if fac else None, "unit": unit, "available": None, "usable": None, "backordered": None, "observed_at": None, "age_days": None, "stale": False, "inventory_status": "MISSING_INVENTORY", "demand_per_day": None, "demand_status": None, "days_of_cover": None, "has_policy": key in pol, "reorder_point": pol[key].reorder_point if key in pol else None, "status": "UNKNOWN", "flags": []}  # fmt: skip
            if p is not None:
                qd = calc.derive_quantities(calc.Position(p.id, key[0], key[1], p.unit, p.observed_at, p.on_hand, p.reserved, p.allocated, p.quarantined, p.damaged, p.usable_source, p.atp_source, p.in_transit, p.backordered), product_unit=prod.unit_of_measure if prod else None, as_of=now, max_age_days=max_age_days, allocated_overlap="UNKNOWN")  # fmt: skip
                avail = qd["available"] if qd["available"] is not None else qd["usable"]
                row.update(available=avail, usable=qd["usable"], backordered=p.backordered, observed_at=qd["observed_at"], age_days=qd["age_days"], stale=qd["stale"], inventory_status=qd["status"])  # fmt: skip
            obs = [
                calc.DemandObs(d.id, d.period_start, d.period_end, d.quantity, d.unit, d.kind.value)
                for d in dem.get(key, [])
            ]
            dm = calc.demand_rate(
                obs,
                unit=unit,
                as_of_date=now.date(),
                window_days=window_days,
                basis="AUTO",
                min_obs=3,
                min_coverage=0.25,
            )
            row.update(demand_per_day=dm["rate_per_day"], demand_status=dm["status"])
            if dm["rate_per_day"] and row["available"] is not None:
                row["days_of_cover"] = round(
                    max(0.0, row["available"] - (p.backordered or 0.0)) / dm["rate_per_day"], 2
                )
            if row["available"] is None:
                row["flags"].append("NO_INVENTORY_VALUE")
                gaps["no_inventory_value"] += 1
            if dm["rate_per_day"] is None:
                row["flags"].append("NO_USABLE_DEMAND")
                gaps["no_demand"] += 1
            if key not in pol:
                row["flags"].append("NO_POLICY")
                gaps["no_policy"] += 1
            if row["stale"]:
                row["flags"].append("STALE_INVENTORY")
                gaps["stale"] += 1
            if row["available"] is not None and row["reorder_point"] is not None:
                row["status"] = (
                    "AT_OR_BELOW_REORDER_POINT"
                    if row["available"] - (p.backordered or 0.0) <= row["reorder_point"]
                    else "ABOVE_REORDER_POINT"
                )
            items.append(row)
    return {"items": items, "total": total, "limit": limit, "offset": offset, "capped": capped, "considered": len(pairs), "gaps_on_page": gaps, "statement": "Status compares available stock less backorders with the registered reorder point only; it ignores open receipts (use a run for the full inventory position). A pair missing demand, policy or inventory is flagged, never treated as healthy."}  # fmt: skip


# ------------------------------------------------------------------------------------------------ cheap calculations
def _calc_request(
    db, request: Request, body: dict[str, Any], *, force_eval: bool = True
) -> tuple[ReplenishRequest, dict[str, Any]]:  # noqa: ANN001
    req = _parse(ReplenishRequest, body)
    if req.scenario_id is not None or not req.what_if.is_neutral() or req.proposed is not None:
        raise DomainValidationError(
            "Scenarios, what-ifs and proposed policies are evaluated by a run (/optimize or /simulate), not by a calculation."
        )
    if force_eval:
        req = req.model_copy(update={"evaluation": "NONE"})
    R.validate_refs(db, req)
    snap = D.load(db, req, req.as_of or utcnow(), _limits(request))
    return req, snap


@router.post(
    "/calc/inventory-position",
    responses=ERR,
    summary="Inventory position now: available - backordered + dated open receipts",
)
def calc_position(body: dict[str, Any], db: DbSession, request: Request, actor: Viewer) -> dict[str, Any]:
    req, snap = _calc_request(db, request, body)
    res = E.compute(snap, req, _limits(request))
    return _hide(actor, {"position": res["position"], "data_quality": res["data_quality"], "snapshot_hash": snap["hash"], "statements": {k: C.STATEMENTS[k] for k in ("decision_support", "missing")}})  # fmt: skip


@router.post(
    "/calc/safety-stock",
    responses=ERR,
    summary="Safety stock for a service target, with its method, assumptions and the data behind it",
)
def calc_safety(body: dict[str, Any], db: DbSession, request: Request, actor: Viewer) -> dict[str, Any]:
    req, snap = _calc_request(db, request, body)
    res = E.compute(snap, req, _limits(request))
    return _hide(actor, {"result_status": res["result_status"], "safety_stock": res["safety_stock"], "demand": res["demand"], "lead_time": res["lead_time"], "order_quantity": res.get("order_quantity"), "infeasibility": res["infeasibility"], "warnings": res["warnings"], "data_quality": res["data_quality"], "sensitivity": res.get("sensitivity"), "snapshot_hash": snap["hash"], "statements": {k: C.STATEMENTS[k] for k in ("service", "safety_stock", "missing")}})  # fmt: skip


@router.post(
    "/calc/reorder-point", responses=ERR, summary="Reorder policy (s, Q, S) and the order due now, without simulation"
)
def calc_policy(body: dict[str, Any], db: DbSession, request: Request, actor: Viewer) -> dict[str, Any]:
    req, snap = _calc_request(db, request, body)
    res = E.compute(snap, req, _limits(request))
    return _hide(actor, {"result_status": res["result_status"], "recommendation": res["recommendation"], "safety_stock": res["safety_stock"], "order_quantity": res.get("order_quantity"), "constraints": res.get("constraints"), "position": res["position"], "projection": res["projection"], "infeasibility": res["infeasibility"], "explanation": res["explanation"], "warnings": res["warnings"], "data_quality": res["data_quality"], "snapshot_hash": snap["hash"], "label": "PROPOSED - decision support, not a purchase order"})  # fmt: skip


# ------------------------------------------------------------------------------------------------ runs
def _queue(
    body: dict[str, Any],
    tasks: BackgroundTasks,
    response: Response,
    db,
    request: Request,
    actor: Actor,
    key: str | None,
    kind: str,
) -> dict[str, Any]:  # noqa: ANN001
    _writes(request)
    req = _parse(ReplenishRequest, body)
    lim = _limits(request)
    with db.begin():
        run, created = R.create(db, req, actor.name, lim, kind=kind, idempotency_key=key)
        if created:
            audit.record(db, actor, "replenishment.run.queued", target_type="REPLENISHMENT_RUN", target_id=run.id, details={"kind": kind, "policy": req.policy_type, "service": req.service_type, "product": req.product_id, "facility": req.facility_id, "scenario": req.scenario_id}, request_id=request_id_of(request))  # fmt: skip
        out, rid = _run(db, run, actor), run.id
    if created:
        tasks.add_task(R.execute, request_session_factory(request), rid, lim)
    else:
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.post(
    "/optimize", status_code=202, responses=ERR, summary="Queue a replenishment optimisation (idempotent with a key)"
)
def optimize(body: dict[str, Any], tasks: BackgroundTasks, response: Response, db: DbSession, request: Request, actor: Editor, idempotency_key: IdemKey = None) -> dict[str, Any]:  # fmt: skip
    return _queue(body, tasks, response, db, request, actor, idempotency_key, "OPTIMIZE")


@router.post(
    "/simulate",
    status_code=202,
    responses=ERR,
    summary="Queue a simulation of an analyst's own policy (set `proposed`) against the current policy",
)
def simulate_policy(body: dict[str, Any], tasks: BackgroundTasks, response: Response, db: DbSession, request: Request, actor: Editor, idempotency_key: IdemKey = None) -> dict[str, Any]:  # fmt: skip
    return _queue(body, tasks, response, db, request, actor, idempotency_key, "SIMULATE")


@router.get("/runs", summary="Run history (newest first)")
def list_runs(
    db: DbSession, actor: Viewer, limit: Lim = 20, offset: Off = 0, status: SimStatus | None = None, kind: Literal["OPTIMIZE", "SIMULATE"] | None = None,
    product_id: Annotated[int | None, Query(ge=1)] = None, facility_id: Annotated[int | None, Query(ge=1)] = None, scenario_id: Annotated[int | None, Query(ge=1)] = None,
    result_status: Annotated[str | None, Query(max_length=24)] = None,
) -> dict[str, Any]:  # fmt: skip
    q = select(ReplenishmentRun)
    for col, v in ((ReplenishmentRun.status, status), (ReplenishmentRun.kind, kind), (ReplenishmentRun.product_id, product_id), (ReplenishmentRun.facility_id, facility_id), (ReplenishmentRun.scenario_id, scenario_id), (ReplenishmentRun.result_status, result_status)):  # fmt: skip
        if v is not None:
            q = q.where(col == v)
    return _page(db, q.order_by(ReplenishmentRun.id.desc()), limit, offset, lambda r: _run(db, r, actor))


@router.get(
    "/runs/{uid}",
    responses=ERR,
    summary="One run: position, demand, lead time, safety stock, recommendation, evaluation, scenarios, provenance and approval state",
)
def get_run(uid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    return _run(db, _run_get(db, uid), actor, full=True)


def _finished(r: ReplenishmentRun) -> ReplenishmentRun:
    if r.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise DomainValidationError(f"The run is {r.status.value}; this section exists only for finished runs.")
    return r


@router.post("/runs/{uid}/cancel", responses=ERR, summary="Cancel a queued run, or ask a running one to stop")
def cancel_run(uid: UidPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        r = _run_get(db, uid)
        if r.requested_by != actor.name and actor.role < Role.REVIEWER:
            raise ForbiddenError("Only the requester or a reviewer can cancel this run.")
        R.cancel(r)
        audit.record(db, actor, "replenishment.run.cancel", target_type="REPLENISHMENT_RUN", target_id=r.id, details={"status": r.status.value}, request_id=request_id_of(request))  # fmt: skip
        return _run(db, r, actor)


@router.get(
    "/runs/{uid}/evaluation",
    responses=ERR,
    summary="Policy comparison by simulation and backtest, with its assumptions",
)
def run_evaluation(uid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    return _hide(actor, {"run_uid": r.run_uid, "evaluation": r.evaluation, "scenario_results": r.scenario_results, "mitigations": r.mitigations, "statements": {k: C.STATEMENTS[k] for k in ("simulation", "optimal", "service")}})  # fmt: skip


@router.get("/runs/{uid}/snapshot", responses=ERR, summary="The exact inputs the engine used (editors and above)")
def run_snapshot(uid: UidPath, db: DbSession, _a: Editor) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    return {"run_uid": r.run_uid, "snapshot_hash": r.snapshot_hash, "snapshot": r.snapshot}


@router.post(
    "/runs/{uid}/replay",
    responses=ERR,
    summary="Recompute a finished run from its stored snapshot and compare with the stored recommendation",
)
def replay_run(uid: UidPath, db: DbSession, request: Request, _a: Editor) -> dict[str, Any]:
    return R.replay(db, _run_get(db, uid), _limits(request))


@router.get("/runs/{uid}/decisions", responses=ERR, summary="Approval history (insert-only)")
def run_decisions(uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _run_get(db, uid)
    rows = db.scalars(
        select(ReplenishmentDecision).where(ReplenishmentDecision.run_id == r.id).order_by(ReplenishmentDecision.id)
    )
    return {"run_uid": r.run_uid, "current": R.approval_state(db, r), "history": [{"decision": d.decision, "rationale": d.rationale, "decided_by": d.decided_by, "decided_at": _iso(d.decided_at), "recommendation_hash": d.recommendation_hash} for d in rows], "statement": "An approval is an internal decision record. ATLAS does not create purchase orders or execute any replenishment."}  # fmt: skip


@router.post(
    "/runs/{uid}/decision",
    responses=ERR,
    summary="Approve or reject a recommended, non-hypothetical run (reviewers and above)",
)
def decide(uid: UidPath, body: DecisionBody, db: DbSession, request: Request, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        r = _run_get(db, uid)
        d = R.decide(db, r, body.decision, body.rationale, actor.name, actor.role >= Role.ADMIN)
        audit.record(db, actor, f"replenishment.run.{body.decision.lower()}", target_type="REPLENISHMENT_RUN", target_id=r.id, details={"recommendation_hash": d.recommendation_hash}, request_id=request_id_of(request))  # fmt: skip
        return {"run_uid": r.run_uid, "approval": R.approval_state(db, r), "recommendation_hash": d.recommendation_hash}
