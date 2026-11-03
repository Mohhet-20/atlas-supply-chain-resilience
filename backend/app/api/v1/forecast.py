"""Demand Forecasting & Time-Series Intelligence API.

Series are derived views over ``demand_observations`` (no second demand store). Runs write only ``forecast_*`` rows (and
audit). Nothing here changes demand, inventory, orders or registry data. Data is limited to the caller's organisation, identity is
the authenticated actor only and request bodies reject unknown fields (including any client-supplied organisation id).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from app.api.v1.deps import DbSession
from app.core.auth import Actor, Role, require
from app.core.errors import ConflictError, DomainValidationError, ForbiddenError, NotFoundError, WriteDisabledError
from app.db.tenancy import request_session_factory
from app.forecast import backtest as bt
from app.forecast import diagnostics as dg
from app.forecast import lifecycle, runs
from app.forecast import metrics as mx
from app.forecast.config import ALGORITHM_VERSION, MODEL_KEYS, STATEMENTS, ForecastConfig
from app.forecast.models import REGISTRY
from app.forecast.pipeline import candidate_list
from app.forecast.prep import prepare
from app.models.enums import SimStatus
from app.models.facility import Facility
from app.models.forecast import ForecastAlert, ForecastEvaluation, ForecastRun, ForecastValue
from app.models.inventory import DemandObservation
from app.models.product import Product
from app.registry import audit
from app.registry.audit import request_id_of
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/forecast", tags=["demand forecasting"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
IdPath = Annotated[int, Path(ge=1, le=2**31 - 1)]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Lim = Annotated[int, Query(ge=1, le=200)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]
Ident = Annotated[int | None, Query(ge=1, le=2**31 - 1)]
IdQ = Annotated[int, Query(ge=1, le=2**31 - 1)]
DISCLAIMER = STATEMENTS["forecast"]


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError("Forecast write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED).")


def _limits(request: Request) -> runs.RunLimits:
    return runs.limits_from(request.app.state.settings)


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


def _get(db, model, ident: int, what: str):  # noqa: ANN001, ANN202
    row = db.get(model, ident)
    if row is None:
        raise NotFoundError(f"{what} not found.")
    return row


def _run(r: ForecastRun, full: bool = False) -> dict[str, Any]:
    out = {
        "id": r.id, "run_uid": r.run_uid, "status": r.status.value, "result_status": r.result_status, "requested_by": r.requested_by,
        "product_id": r.product_id, "facility_id": r.facility_id, "frequency": r.frequency, "horizon": r.horizon, "unit": r.unit,
        "origin": _iso(r.origin), "selected_model": r.selected_model, "model_version": r.model_version, "algorithm_version": r.algorithm_version,
        "config_hash": r.config_hash, "data_snapshot_hash": r.data_snapshot_hash, "provenance": r.provenance, "queued_at": _iso(r.queued_at),
        "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at), "duration_ms": r.duration_ms, "error": r.error,
        "cancel_requested": r.cancel_requested, "fallback": r.fallback, "selection_status": (r.selection or {}).get("status"),
        "patterns": (r.patterns or {}).get("labels"), "disclaimer": DISCLAIMER,
    }  # fmt: skip
    if full:
        out |= {
            "config": r.config, "model_params": r.model_params, "training_interval": r.training_interval, "preparation": r.preparation,
            "diagnostics": r.diagnostics, "patterns_detail": r.patterns, "candidates": r.candidates, "selection": r.selection,
            "validation": r.validation, "clamping": r.clamping, "reproducibility": r.reproducibility, "timings": r.timings,
            "warnings": r.warnings, "statements": STATEMENTS,
        }  # fmt: skip
    return out


def _value(v: ForecastValue, run: ForecastRun) -> dict[str, Any]:
    return {"step": v.step, "period_start": _iso(v.period_start), "period_end": _iso(v.period_end), "point": v.point, "point_raw": v.point_raw, "clamped_at_zero": v.point != v.point_raw, "intervals": v.intervals, "unit": run.unit, "model": run.selected_model, "model_version": run.model_version, "origin": _iso(run.origin), "kind": "forecast"}  # fmt: skip


def _eval(e: ForecastEvaluation, full: bool = False) -> dict[str, Any]:
    out = {"id": e.id, "run_id": e.run_id, "evaluated_at": _iso(e.evaluated_at), "requested_by": e.requested_by, "status": e.status, "n_matched": e.n_matched, "n_final": e.n_final, "actuals_hash": e.actuals_hash, "metrics": e.metrics}  # fmt: skip
    if full:
        out["matches"] = e.matches
    return out


def _alert(a: ForecastAlert) -> dict[str, Any]:
    return {"id": a.id, "kind": a.kind, "severity": a.severity, "product_id": a.product_id, "facility_id": a.facility_id, "run_id": a.run_id, "message": a.message, "evidence": a.evidence, "created_at": _iso(a.created_at)}  # fmt: skip


def _finished(r: ForecastRun) -> ForecastRun:
    if r.status in (SimStatus.QUEUED, SimStatus.RUNNING):
        raise ConflictError(f"Run is {r.status.value}; results are available when it finishes.")
    if r.status in (SimStatus.FAILED, SimStatus.CANCELLED):
        raise ConflictError(f"Run {r.status.value.lower()}; it has no results.")
    return r


def _values_of(db, run_id: int) -> list[ForecastValue]:  # noqa: ANN001
    return list(db.scalars(select(ForecastValue).where(ForecastValue.run_id == run_id).order_by(ForecastValue.step)))


def _conf(raw: dict[str, Any]) -> ForecastConfig:
    try:
        return ForecastConfig.model_validate(raw)
    except ValidationError as e:
        raise DomainValidationError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e


# ------------------------------------------------------------------------------------------------ reference


@router.get("/limits", summary="Server limits, configuration defaults, model registry and fixed statements")
def limits(request: Request, _a: Viewer) -> dict[str, Any]:
    s = request.app.state.settings
    return {
        "algorithm_version": ALGORITHM_VERSION, "max_horizon_daily": s.forecast_max_horizon_daily, "max_horizon_weekly": s.forecast_max_horizon_weekly,
        "max_history_periods": s.forecast_max_history_periods, "max_folds": s.forecast_max_folds, "max_candidates": s.forecast_max_candidates,
        "max_active_runs": s.forecast_max_active_runs, "time_budget_seconds": s.forecast_time_budget_seconds, "finalization_days": s.forecast_finalization_days,
        "defaults": ForecastConfig().canonical(), "models": list(MODEL_KEYS), "metrics": mx.FORMULAS, "statements": STATEMENTS,
        "write_enabled": s.write_api_allowed, "organisation_model": "none — ATLAS has no tenancy; access is by authenticated role only",
    }  # fmt: skip


@router.get(
    "/models", summary="The closed model registry: eligibility rules, minimum history, interval support, versions"
)
def models(_a: Viewer) -> dict[str, Any]:
    return {"items": [m.describe() for m in REGISTRY.values()], "note": "Only these models can be requested; no code or expressions are accepted. Fitting is deterministic and uses the Python standard library only."}  # fmt: skip


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any] = Field(default_factory=dict)


@router.post(
    "/config/validate", summary="Validate a configuration against the schema and the server limits (nothing is stored)"
)
def validate_config(body: ConfigBody, request: Request, _a: Viewer) -> dict[str, Any]:
    try:
        conf = ForecastConfig.model_validate(body.config)
    except ValidationError as e:
        return {
            "valid": False,
            "errors": [{"field": ".".join(str(x) for x in err["loc"]), "message": err["msg"]} for err in e.errors()],
            "config": None,
        }
    try:
        conf.check_limits(_limits(request).fc)
    except ValueError as e:
        return {"valid": False, "errors": [{"field": "limits", "message": str(e)}], "config": conf.canonical()}
    return {
        "valid": True,
        "errors": [],
        "config": conf.canonical(),
        "config_digest": conf.digest(),
        "seasonal_period_used": conf.season(),
    }


# ------------------------------------------------------------------------------------------------ series


@router.get(
    "/series", summary="Product-facility demand series available for forecasting (derived from demand observations)"
)
def list_series(db: DbSession, _a: Viewer, limit: Lim = 25, offset: Off = 0, product_id: Ident = None, facility_id: Ident = None, q: Annotated[str | None, Query(max_length=100)] = None) -> dict[str, Any]:  # fmt: skip
    D = DemandObservation
    base = select(D.product_id, D.facility_id, func.count().label("n"), func.min(D.period_start).label("first"), func.max(D.period_end).label("last")).group_by(D.product_id, D.facility_id)  # fmt: skip
    if product_id:
        base = base.where(D.product_id == product_id)
    if facility_id:
        base = base.where(D.facility_id == facility_id)
    sub = base.subquery()
    stmt = select(sub, Product.name.label("pname"), Product.product_code, Facility.name.label("fname"), Facility.facility_code).join(Product, Product.id == sub.c.product_id).join(Facility, Facility.id == sub.c.facility_id)  # fmt: skip
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Product.name).like(like)
            | func.lower(Facility.name).like(like)
            | func.lower(Product.product_code).like(like)
        )
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.execute(stmt.order_by(sub.c.product_id, sub.c.facility_id).limit(limit).offset(offset)).all()
    items = []
    for r in rows:
        kinds = db.execute(select(D.kind, D.unit, D.provenance, func.count()).where(D.product_id == r.product_id, D.facility_id == r.facility_id).group_by(D.kind, D.unit, D.provenance)).all()  # fmt: skip
        last = db.scalar(select(ForecastRun).where(ForecastRun.product_id == r.product_id, ForecastRun.facility_id == r.facility_id).order_by(ForecastRun.id.desc()).limit(1))  # fmt: skip
        items.append({
            "product_id": r.product_id, "facility_id": r.facility_id, "product_code": r.product_code, "product_name": r.pname, "facility_code": r.facility_code,
            "facility_name": r.fname, "target": "demand", "observations": r.n, "first_period": _iso(r.first), "last_period": _iso(r.last),
            "kinds": sorted({k.value for k, *_ in kinds}), "units": sorted({u for _, u, *_ in kinds}), "provenance": sorted({p.value for _, _, p, _ in kinds}),
            "aggregation": "daily or weekly buckets; multi-day observations are allocated pro-rata", "latest_run": _run(last) if last else None,
        })  # fmt: skip
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _prepared(db, request: Request, product_id: int, facility_id: int, conf: ForecastConfig):  # noqa: ANN001, ANN202
    lim = _limits(request)
    try:
        conf.check_limits(lim.fc)
    except ValueError as e:
        raise DomainValidationError(str(e)) from e
    _get(db, Product, product_id, "Product")
    _get(db, Facility, facility_id, "Facility")
    obs = runs.load_obs(db, product_id, facility_id, lim.fc.max_observations)
    return prepare(obs, conf, lim.fc), lim


@router.get(
    "/series/diagnostics",
    responses=ERR,
    summary="Prepared series, data-quality report, diagnostics and pattern classification for one product at one facility",
)
def series_diagnostics(
    request: Request, db: DbSession, _a: Viewer, product_id: IdQ, facility_id: IdQ,
    frequency: Annotated[str | None, Query(pattern="^(daily|weekly)$")] = None, lookback_periods: Annotated[int | None, Query(ge=8, le=3650)] = None,
    basis: Annotated[str | None, Query(pattern="^(AUTO|CONSUMPTION|DEMAND|SALES)$")] = None, unit: Annotated[str | None, Query(max_length=32)] = None,
    seasonal_period: Annotated[int | None, Query(ge=2, le=60)] = None, missing_policy: Annotated[str | None, Query(pattern="^(reject|interpolate_linear|carry_forward)$")] = None,
    outlier_handling: Annotated[str | None, Query(pattern="^(preserve|flag|transform|exclude)$")] = None,
) -> dict[str, Any]:  # fmt: skip
    kw = dict(frequency=frequency, lookback_periods=lookback_periods, basis=basis, unit=unit, seasonal_period=seasonal_period, missing_policy=missing_policy, outlier_handling=outlier_handling)  # fmt: skip
    conf = _conf({k: v for k, v in kw.items() if v is not None})
    p, lim = _prepared(db, request, product_id, facility_id, conf)
    out: dict[str, Any] = {"preparation": p.summary(), "statements": STATEMENTS}
    if p.dates:
        out["points"] = [{"period_start": d.isoformat(), "raw": p.raw[i], "value": p.values[i] if p.values else None, "observed": bool(p.observed[i]) if p.observed else p.raw[i] is not None, "imputed": bool(p.values) and p.raw[i] is None} for i, d in enumerate(p.dates)]  # fmt: skip
    if p.usable:
        diag = dg.diagnose(p, conf.season())
        out["diagnostics"], out["patterns"] = diag, dg.classify(diag, lim.fc.min_history_periods)
    else:
        label = "insufficient_history" if p.status in ("insufficient_data", "no_observations") else "poor_data_quality"
        out["diagnostics"], out["patterns"] = None, {"labels": [label], "evidence": {label: {"status": p.status, "warnings": p.warnings}}, "criteria": dg.CRITERIA}  # fmt: skip
    return out


@router.get(
    "/series/patterns", responses=ERR, summary="Demand pattern labels with the criteria and evidence behind each"
)
def series_patterns(
    request: Request, db: DbSession, _a: Viewer, product_id: IdQ, facility_id: IdQ,
    frequency: Annotated[str | None, Query(pattern="^(daily|weekly)$")] = None, lookback_periods: Annotated[int | None, Query(ge=8, le=3650)] = None,
) -> dict[str, Any]:  # fmt: skip
    d = series_diagnostics(
        request, db, _a, product_id, facility_id, frequency, lookback_periods, None, None, None, None, None
    )
    return {"patterns": d["patterns"], "preparation_status": d["preparation"]["status"]}


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(ge=1, le=2**31 - 1)
    facility_id: int = Field(ge=1, le=2**31 - 1)
    config: dict[str, Any] = Field(default_factory=dict)


@router.post(
    "/candidates/preview",
    responses=ERR,
    summary="Which models are eligible for this series and what the validation plan would be (nothing is fitted)",
)
def preview(body: PreviewBody, request: Request, db: DbSession, _a: Viewer) -> dict[str, Any]:
    conf = _conf(body.config)
    p, lim = _prepared(db, request, body.product_id, body.facility_id, conf)
    if not p.usable:
        return {
            "preparation_status": p.status,
            "warnings": p.warnings,
            "eligible": [],
            "ineligible": {},
            "models": {},
            "validation_plan": None,
        }
    season = conf.season()
    ok, why = candidate_list(conf, p, season)
    plan = bt.plan_folds(len(p.values), conf, lim.fc, season)
    allm = {
        k: ({"eligible": True} if k in ok else {"eligible": False, "reason": why.get(k)})
        for k in (conf.candidates or MODEL_KEYS)
    }
    return {"preparation_status": p.status, "periods": len(p.values), "eligible": ok, "ineligible": why, "models": allm, "validation_plan": {"folds": len(plan.origins), "fold_train_lengths": plan.origins, "evaluation_horizon": plan.horizon, "min_train_periods": plan.min_train, "status": plan.status, "note": plan.note}}  # fmt: skip


# ------------------------------------------------------------------------------------------------ runs


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(ge=1, le=2**31 - 1)
    facility_id: int = Field(ge=1, le=2**31 - 1)
    config: ForecastConfig = Field(default_factory=ForecastConfig)


@router.post(
    "/runs",
    status_code=202,
    responses={**ERR, 202: {"description": "Queued"}},
    summary="Queue a forecast run (idempotent with a key)",
)
def create_run(
    body: RunCreate, tasks: BackgroundTasks, response: Response, db: DbSession, request: Request, actor: Editor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")] = None,
) -> dict[str, Any]:  # fmt: skip
    _writes(request)
    lim = _limits(request)
    with db.begin():
        r, created = runs.create_run(
            db, body.product_id, body.facility_id, body.config, actor.name, lim, idempotency_key=idempotency_key
        )
        if created:
            audit.record(db, actor, "forecast.run.queued", target_type="FORECAST_RUN", target_id=r.id, details={"product_id": r.product_id, "facility_id": r.facility_id, "config_hash": r.config_hash}, request_id=request_id_of(request))  # fmt: skip
        out = _run(r)
    if created:
        tasks.add_task(runs.execute_run, request_session_factory(request), out["id"], lim)
    else:
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.get("/runs", summary="Forecast runs (newest first)")
def list_runs(
    db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0, product_id: Ident = None, facility_id: Ident = None, status: SimStatus | None = None,
    result_status: Annotated[str | None, Query(max_length=40)] = None, model: Annotated[str | None, Query(max_length=40)] = None,
) -> dict[str, Any]:  # fmt: skip
    q = select(ForecastRun)
    for col, v in ((ForecastRun.product_id, product_id), (ForecastRun.facility_id, facility_id), (ForecastRun.status, status), (ForecastRun.result_status, result_status), (ForecastRun.selected_model, model)):  # fmt: skip
        if v is not None:
            q = q.where(col == v)
    return _page(db, q.order_by(ForecastRun.id.desc()), limit, offset, _run)


@router.get("/runs/compare", responses=ERR, summary="Side-by-side summary of up to four finished runs")
def compare(
    db: DbSession, _a: Viewer, ids: Annotated[str, Query(min_length=1, max_length=40, pattern=r"^\d+(,\d+){0,3}$")]
) -> dict[str, Any]:
    items = []
    for i in dict.fromkeys(int(x) for x in ids.split(",")):
        r = _finished(_get(db, ForecastRun, i, "Run"))
        ev = ((r.evaluation or {}).get(r.selected_model or "") or {}).get("overall") or {}
        items.append({**_run(r), "backtest_overall": {k: (ev.get(k) or {}).get("value") for k in mx.FORMULAS}, "values": [{"step": v.step, "period_start": _iso(v.period_start), "point": v.point} for v in _values_of(db, i)]})  # fmt: skip
    return {
        "items": items,
        "note": "Runs with different origins, units or configurations are not directly comparable; compare like with like.",
    }


@router.get(
    "/runs/{run_id}",
    responses=ERR,
    summary="Run status, configuration, data preparation, diagnostics, selection and reproducibility",
)
def get_run(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _run(_get(db, ForecastRun, run_id, "Run"), full=True)


@router.post("/runs/{run_id}/cancel", responses=ERR, summary="Cancel a queued run, or ask a running one to stop")
def cancel(run_id: IdPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        r = _get(db, ForecastRun, run_id, "Run")
        if r.requested_by != actor.name and actor.role < Role.REVIEWER:
            raise ForbiddenError("Only the requester or a reviewer can cancel this run.")
        runs.cancel_run(db, r)
        audit.record(db, actor, "forecast.run.cancel", target_type="FORECAST_RUN", target_id=r.id, details={"status": r.status.value}, request_id=request_id_of(request))  # fmt: skip
        return _run(r)


@router.get(
    "/runs/{run_id}/values",
    responses=ERR,
    summary="Forecast values (unrounded) with unit, model version and origin; intervals only where supported",
)
def values(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _finished(_get(db, ForecastRun, run_id, "Run"))
    vs = _values_of(db, run_id)
    return {"run": _run(r), "items": [_value(v, r) for v in vs], "total": len(vs), "kind": "forecast", "disclaimer": DISCLAIMER, "clamping": r.clamping, "note": None if vs else "This run declined to forecast; see the rationale."}  # fmt: skip


@router.get(
    "/runs/{run_id}/intervals",
    responses=ERR,
    summary="Interval method, calibration evidence, empirical coverage and the bounds per step",
)
def intervals(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _finished(_get(db, ForecastRun, run_id, "Run"))
    cal = [{k: v for k, v in c.items() if k != "per_horizon"} for c in (r.interval_calibration or [])]
    return {"run_id": run_id, "calibration": cal, "bounds": [{"step": v.step, "period_start": _iso(v.period_start), "intervals": v.intervals} for v in _values_of(db, run_id)], "statement": STATEMENTS["interval"], "model": r.selected_model, "model_version": r.model_version, "assumptions": ["Residuals are exchangeable across folds and horizons within a band.", "Future error behaviour resembles the backtest period.", "Offsets are absolute (not relative to level)."]}  # fmt: skip


@router.get(
    "/runs/{run_id}/rationale",
    responses=ERR,
    summary="Why this model was selected (or why none was), with the evidence",
)
def rationale(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _finished(_get(db, ForecastRun, run_id, "Run"))
    return {"run_id": run_id, "result_status": r.result_status, "selection": r.selection, "fallback": r.fallback, "patterns": r.patterns, "candidates": r.candidates, "validation": r.validation, "warnings": r.warnings, "statements": STATEMENTS}  # fmt: skip


@router.get(
    "/runs/{run_id}/metrics",
    responses=ERR,
    summary="Per-model backtest metrics: overall, per fold and per horizon, with formulas",
)
def metrics(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _finished(_get(db, ForecastRun, run_id, "Run"))
    return {"run_id": run_id, "formulas": {k: {"formula": a, "undefined_when": b} for k, (a, b) in mx.FORMULAS.items()}, "bias_convention": "forecast − actual; positive = over-forecast", "models": r.evaluation or {}, "selected": r.selected_model, "ranking": (r.selection or {}).get("ranking")}  # fmt: skip


@router.get("/runs/{run_id}/backtests", responses=ERR, summary="Validation scheme, fold origins and per-fold results")
def backtests(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _finished(_get(db, ForecastRun, run_id, "Run"))
    folds = {
        k: {"per_fold": v.get("per_fold"), "failed_folds": v.get("failed_folds"), "seconds": v.get("seconds")}
        for k, v in (r.evaluation or {}).items()
    }
    return {"run_id": run_id, "validation": r.validation, "models": folds}


@router.get("/runs/{run_id}/failures", responses=ERR, summary="Run error, model failures and warnings")
def failures(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _get(db, ForecastRun, run_id, "Run")
    mf = [{"model": k, **f} for k, v in (r.evaluation or {}).items() for f in v.get("failures", [])]
    return {
        "run_id": run_id,
        "status": r.status.value,
        "error": r.error,
        "model_failures": mf,
        "ineligible": (r.candidates or {}).get("ineligible"),
        "warnings": r.warnings,
    }


@router.get(
    "/runs/{run_id}/inventory-eligibility",
    responses=ERR,
    summary="Whether this run can drive an inventory projection, and the conditions",
)
def inventory_eligibility(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _get(db, ForecastRun, run_id, "Run")
    vals = _values_of(db, run_id)
    ok = r.result_status in runs.OK_RESULTS and bool(vals)
    return {
        "run_id": run_id, "selectable": ok, "reason": None if ok else f"The run produced no forecast values ({r.result_status or r.status.value}).",
        "unit": r.unit, "frequency": r.frequency, "origin": _iso(r.origin), "covers_from": _iso(vals[0].period_start) if vals else None,
        "covers_to_exclusive": _iso(vals[-1].period_end) if vals else None, "validated": bool(r.result_status == "completed" and not r.fallback),
        "product_id": r.product_id, "facility_id": r.facility_id,
        "conditions": ["The inventory unit must match the forecast unit, or an explicit conversion factor must be supplied (forecast_conversion_factor).", "Only the pair this run was made for uses the forecast; other pairs keep the observed rate.", "The projection stops where the forecast ends.", "The supply-disruption scenario stays separate from forecast demand."],  # fmt: skip
        "how": "POST /inventory/analyses with config.forecast_run_id",
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ actuals & monitoring


@router.get(
    "/runs/{run_id}/forecast-vs-actual",
    responses=ERR,
    summary="Match the issued forecast to later demand observations (read-only; nothing is stored)",
)
def forecast_vs_actual(run_id: IdPath, request: Request, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _finished(_get(db, ForecastRun, run_id, "Run"))
    vs = _values_of(db, run_id)
    if not vs:
        raise DomainValidationError("This run produced no forecast values to compare.")
    fin = _limits(request).finalization_days
    ms = lifecycle.actual_periods(db, r, vs, datetime.now(timezone.utc).date(), fin)
    scale = (r.validation or {}).get("naive_scale")
    counts = {s: sum(1 for m in ms if m["status"] == s) for s in ("final", "preliminary", "partial", "missing")}
    return {
        "run_id": run_id, "origin": _iso(r.origin), "finalization_days": fin, "matches": ms, "unit": r.unit, "counts": counts,
        "metrics": {"final": lifecycle._score(ms, ("final",), scale), "interval_coverage": lifecycle._coverage(ms)} if counts["final"] else None,
        "note": "Final = fully observed and older than the finalization window; preliminary actuals may still be revised. The issued forecast is never changed.",
    }  # fmt: skip


@router.post(
    "/runs/{run_id}/evaluations",
    status_code=201,
    responses=ERR,
    summary="Store a forecast-versus-actual snapshot (same actuals → the existing snapshot)",
)
def create_evaluation(
    run_id: IdPath, request: Request, response: Response, db: DbSession, actor: Editor
) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        r = _finished(_get(db, ForecastRun, run_id, "Run"))
        try:
            ev, created = lifecycle.evaluate_run(db, r, actor.name, _limits(request).finalization_days)
        except ValueError as e:
            raise DomainValidationError(str(e)) from e
        if created:
            audit.record(db, actor, "forecast.evaluation.create", target_type="FORECAST_EVALUATION", target_id=ev.id, details={"run_id": run_id, "status": ev.status}, request_id=request_id_of(request))  # fmt: skip
        else:
            response.status_code = 200
        return _eval(ev, full=True)


@router.get("/runs/{run_id}/evaluations", responses=ERR, summary="Stored forecast-versus-actual snapshots for a run")
def list_evaluations(run_id: IdPath, db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0) -> dict[str, Any]:
    _get(db, ForecastRun, run_id, "Run")
    return _page(
        db,
        select(ForecastEvaluation).where(ForecastEvaluation.run_id == run_id).order_by(ForecastEvaluation.id.desc()),
        limit,
        offset,
        _eval,
    )


@router.get("/performance", summary="Performance history: stored evaluation snapshots across runs of a series")
def performance(
    db: DbSession, _a: Viewer, product_id: Ident = None, facility_id: Ident = None, limit: Lim = 20, offset: Off = 0
) -> dict[str, Any]:
    q = select(ForecastEvaluation).join(ForecastRun, ForecastRun.id == ForecastEvaluation.run_id)
    if product_id:
        q = q.where(ForecastRun.product_id == product_id)
    if facility_id:
        q = q.where(ForecastRun.facility_id == facility_id)
    return _page(db, q.order_by(ForecastEvaluation.id.desc()), limit, offset, _eval)


@router.post(
    "/monitoring/check",
    responses=ERR,
    summary="Evaluate the latest run of every series and record new alerts (never retrains)",
)
def monitoring_check(request: Request, db: DbSession, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        found = lifecycle.monitor(db, actor.name, _limits(request).finalization_days)
        audit.record(db, actor, "forecast.monitoring.check", target_type="FORECAST_MONITORING", target_id=0, details={"new_alerts": len(found)}, request_id=request_id_of(request))  # fmt: skip
        db.flush()
        return {"new_alerts": [_alert(a) for a in found], "count": len(found), "note": "Monitoring raises signals only. It never retrains, replaces or edits a forecast.", "minimum_final_periods": lifecycle.MIN_EVAL_N}  # fmt: skip


@router.get("/alerts", summary="Monitoring alerts (newest first)")
def list_alerts(db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0, product_id: Ident = None, facility_id: Ident = None, kind: Annotated[str | None, Query(max_length=30)] = None) -> dict[str, Any]:  # fmt: skip
    q = select(ForecastAlert)
    for col, v in (
        (ForecastAlert.product_id, product_id),
        (ForecastAlert.facility_id, facility_id),
        (ForecastAlert.kind, kind),
    ):
        if v is not None:
            q = q.where(col == v)
    return _page(db, q.order_by(ForecastAlert.id.desc()), limit, offset, _alert)
