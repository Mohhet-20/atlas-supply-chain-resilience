"""Lead-Time Prediction, Delivery Reliability & Delay Risk Intelligence API.

Delivery records are append-only versions plus optional events; every statistic is derived on read. Runs write only
``leadtime_runs`` (and audit); assessments are insert-only. Nothing here changes inventory, orders, suppliers or the registry.
Data is limited to the caller's organisation, identity is the authenticated actor only and bodies reject unknown fields.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import func, select

from app.api.v1.deps import DbSession
from app.api.v1.ingestion import _format_of, _read_body, _require_write
from app.api.v1.inventory import _mapping
from app.core.auth import Actor, Role, require
from app.core.errors import ConflictError, DomainValidationError, ForbiddenError, NotFoundError, WriteDisabledError
from app.db.tenancy import request_session_factory
from app.ingestion.constants import ALLOWED_DATE_FORMATS
from app.leadtime import analytics, ingest, runs
from app.leadtime import semantics as sem
from app.leadtime.assess import AssessRequest, assess, outcome_of
from app.leadtime.config import (
    ALGORITHM_VERSION,
    DEFINITIONS,
    LEADTIME_MODELS,
    RISK_MODELS,
    STATEMENTS,
    LeadTimeConfig,
)  # fmt: skip
from app.leadtime.estimators import REGISTRY
from app.leadtime.resolve import load_deliveries, summary_of
from app.models.delivery import (
    DeliveryAssessment,
    DeliveryEvent,
    DeliveryImportBatch,
    DeliveryImportKind,
    DeliveryImportRow,
    DeliveryRowOutcome,
    DeliverySnapshot,
    LeadTimeRun,
)  # fmt: skip
from app.models.enums import Provenance, RiskCategory, SimStatus, SourceFormat
from app.models.risk import RiskAssessment, RiskFactor
from app.registry import audit
from app.registry.audit import request_id_of
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/leadtime", tags=["lead time and delivery risk"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
IdPath = Annotated[int, Path(ge=1, le=2**31 - 1)]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Lim = Annotated[int, Query(ge=1, le=200)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]
Ident = Annotated[int | None, Query(ge=1, le=2**31 - 1)]


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError(
            "Lead-time write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED)."
        )


def _limits(request: Request) -> runs.LeadTimeLimits:
    return runs.limits_from(request.app.state.settings)


def _iso(d: Any) -> str | None:
    return None if d is None else d.isoformat()


def _get(db, model, ident: int, what: str):  # noqa: ANN001, ANN202
    row = db.get(model, ident)
    if row is None:
        raise NotFoundError(f"{what} not found.")
    return row


def _page(db, q, limit, offset, shape) -> dict[str, Any]:  # noqa: ANN001
    total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0
    return {"items": [shape(r) for r in db.scalars(q.limit(limit).offset(offset))], "total": total, "limit": limit, "offset": offset}  # fmt: skip


def _conf(raw: dict[str, Any]) -> LeadTimeConfig:
    try:
        return LeadTimeConfig.model_validate(raw)
    except ValidationError as e:
        raise DomainValidationError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e


def _batch(b: DeliveryImportBatch) -> dict[str, Any]:
    return {
        "id": b.id, "batch_uid": b.batch_uid, "kind": b.kind.value, "status": b.status.value, "source_label": b.source_label,
        "provenance": b.provenance.value, "file_name": b.file_name, "file_format": b.file_format, "requested_by": b.requested_by,
        "time_zone": b.time_zone, "mapping": b.mapping, "rows_total": b.rows_total, "rows_accepted": b.rows_accepted,
        "rows_duplicate": b.rows_duplicate, "rows_rejected": b.rows_rejected, "summary": b.summary, "created_at": _iso(b.created_at),
    }  # fmt: skip


def _run(r: LeadTimeRun, full: bool = False) -> dict[str, Any]:
    out = {
        "id": r.id, "run_uid": r.run_uid, "status": r.status.value, "result_status": r.result_status, "requested_by": r.requested_by,
        "target": r.target, "selected_model": r.selected_model, "algorithm_version": r.algorithm_version, "config_hash": r.config_hash,
        "data_snapshot_hash": r.data_snapshot_hash, "as_of": _iso(r.as_of), "provenance": r.provenance, "queued_at": _iso(r.queued_at),
        "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at), "duration_ms": r.duration_ms, "error": r.error,
        "cancel_requested": r.cancel_requested, "selection_status": (r.selection or {}).get("status"),
        "score_kind": (r.artifact or {}).get("score_kind"), "statement": STATEMENTS["score"] if r.target == "LATE_RISK" else STATEMENTS["evidence"],  # fmt: skip
    }  # fmt: skip
    if full:
        out |= {
            "config": r.config, "artifact": r.artifact, "preparation": r.preparation, "splits": r.splits, "candidates": r.candidates,
            "evaluation": r.evaluation, "selection": r.selection, "calibration": r.calibration, "intervals": r.intervals,
            "explanation": r.explanation, "leakage_controls": r.leakage_controls, "reproducibility": r.reproducibility,
            "timings": r.timings, "warnings": r.warnings, "failures": r.failures, "statements": STATEMENTS,
        }  # fmt: skip
    return out


def _assessment(a: DeliveryAssessment, full: bool = False) -> dict[str, Any]:
    out = {
        "id": a.id, "assessment_uid": a.assessment_uid, "subject": a.subject, "delivery_ref": a.delivery_ref, "product_id": a.product_id,
        "supplier_id": a.supplier_id, "destination_facility_id": a.destination_facility_id, "prediction_point": a.prediction_point,
        "reference_at": _iso(a.reference_at), "as_of": _iso(a.as_of), "status": a.status, "unavailable_reason": a.unavailable_reason,
        "commitment": a.commitment, "promised_at": _iso(a.promised_at), "unit": a.unit, "leadtime_run_id": a.leadtime_run_id,
        "risk_run_id": a.risk_run_id, "estimate": a.estimate, "arrival": a.arrival, "risk": a.risk, "algorithm_version": a.algorithm_version,
        "created_at": _iso(a.created_at), "requested_by": a.requested_by, "statement": STATEMENTS["decision"],
    }  # fmt: skip
    if full:
        out |= {
            "history": a.history,
            "factors": a.factors,
            "evidence": a.evidence,
            "warnings": a.warnings,
            "input_hash": a.input_hash,
        }
    return out


def _finished(r: LeadTimeRun) -> LeadTimeRun:
    if r.status in (SimStatus.QUEUED, SimStatus.RUNNING):
        raise ConflictError(f"Run is {r.status.value}; results are available when it finishes.")
    if r.status in (SimStatus.FAILED, SimStatus.CANCELLED):
        raise ConflictError(f"Run {r.status.value.lower()}; it has no results.")
    return r


def _cfg_from_query(**kw: Any) -> LeadTimeConfig:
    return _conf({k: v for k, v in kw.items() if v is not None})


def _as_of(v: datetime | None) -> datetime:
    if v is None:
        return datetime.now(timezone.utc)
    if v.tzinfo is None:
        raise DomainValidationError("as_of must carry a time zone.")
    return v


# ------------------------------------------------------------------------------------------------ reference


@router.get("/limits", summary="Server limits, definitions, model registry and fixed statements")
def limits(request: Request, _a: Viewer) -> dict[str, Any]:
    s = request.app.state.settings
    return {
        "algorithm_version": ALGORITHM_VERSION, "max_import_rows": s.leadtime_max_import_rows, "max_records_per_run": s.leadtime_max_records_per_run,
        "max_active_runs": s.leadtime_max_active_runs, "time_budget_seconds": s.leadtime_time_budget_seconds, "definitions": list(DEFINITIONS),
        "import_fields": {k.value: ingest.fields_of(k) for k in DeliveryImportKind}, "date_formats": list(ALLOWED_DATE_FORMATS),
        "write_enabled": s.write_api_allowed, "statements": STATEMENTS,
        "tenancy": "Data is separated by organisation: every query is limited to the caller's organisation; within it, data is visible to every user at or above the endpoint role.",
    }  # fmt: skip


@router.get("/models", summary="Closed model registry with eligibility rules")
def models(_a: Viewer) -> dict[str, Any]:
    return {"lead_time": [{"key": k, **REGISTRY[k]} for k in LEADTIME_MODELS], "late_risk": [{"key": k, **REGISTRY[k]} for k in RISK_MODELS]}  # fmt: skip


@router.post("/config/validate", summary="Validate a run configuration without running anything")
def validate_config(body: dict[str, Any], _a: Viewer) -> dict[str, Any]:
    c = _conf(body)
    return {"valid": True, "canonical": c.canonical(), "config_hash": c.digest()}


# ------------------------------------------------------------------------------------------------ imports


@router.post("/imports", responses=ERR, summary="Preview or commit a CSV/JSON import of deliveries or delivery events")
async def import_file(
    request: Request, db: DbSession, actor: Editor, kind: DeliveryImportKind, mode: Literal["preview", "commit"] = "preview",
    format: Annotated[Literal["CSV", "JSON"] | None, Query()] = None, filename: Annotated[str | None, Query(max_length=255)] = None,
    mapping: Annotated[str | None, Query(max_length=4000, description="JSON object: canonical field -> file column.")] = None,
    source_label: Annotated[str, Query(min_length=1, max_length=200)] = "uploaded file",
    provenance: Provenance = Provenance.USER_PROVIDED, atomic: bool = False, date_format: str = "%Y-%m-%d",
    time_zone: Annotated[str, Query(max_length=64, description="IANA zone for timestamps without an offset.")] = "UTC",
    delimiter: Annotated[str, Query(max_length=8)] = ",", encoding: Annotated[str | None, Query(max_length=20)] = None,
    chronology_tolerance_hours: Annotated[float, Query(ge=0, le=72)] = 0.0,
) -> dict[str, Any]:  # fmt: skip
    if mode == "commit":
        _require_write(request)
    data = await _read_body(request)
    if not data:
        raise DomainValidationError("The request body is empty. Send the file bytes as the body.")
    fmt = _format_of(request, SourceFormat(format) if format else None, filename)
    kw = dict(
        fmt=fmt, encoding=encoding, delimiter=delimiter, mapping=_mapping(mapping), time_zone=time_zone, date_format=date_format,
        max_rows=request.app.state.settings.leadtime_max_import_rows, chronology_tolerance_hours=chronology_tolerance_hours,
    )  # fmt: skip
    if mode == "preview":
        return ingest.preview(db, kind, data, **kw)
    with db.begin():
        batch, created, _prep = ingest.commit(db, kind, data, source_label=source_label, provenance=provenance, requested_by=actor.name, file_name=filename, atomic=atomic, **kw)  # fmt: skip
        if created:
            audit.record(db, actor, "leadtime.import", target_type="DELIVERY_IMPORT", target_id=batch.id, details={"kind": kind.value, "accepted": batch.rows_accepted, "rejected": batch.rows_rejected, "duplicate": batch.rows_duplicate}, request_id=request_id_of(request))  # fmt: skip
        out = _batch(batch)
    return {"mode": "commit", "committed": out["rows_accepted"] > 0, "created": created, "replayed_existing_batch": not created, "batch": out}  # fmt: skip


@router.get("/imports", summary="Import batches (newest first)")
def list_imports(
    db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0, kind: DeliveryImportKind | None = None
) -> dict[str, Any]:
    q = select(DeliveryImportBatch)
    if kind:
        q = q.where(DeliveryImportBatch.kind == kind)
    return _page(db, q.order_by(DeliveryImportBatch.id.desc()), limit, offset, _batch)


@router.get("/imports/{batch_id}", responses=ERR, summary="One import batch")
def get_import(batch_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _batch(_get(db, DeliveryImportBatch, batch_id, "Import batch"))


@router.get("/imports/{batch_id}/rows", responses=ERR, summary="Row-level outcomes and errors for a batch")
def import_rows(batch_id: IdPath, db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0, outcome: DeliveryRowOutcome | None = None) -> dict[str, Any]:  # fmt: skip
    _get(db, DeliveryImportBatch, batch_id, "Import batch")
    q = select(DeliveryImportRow).where(DeliveryImportRow.batch_id == batch_id)
    if outcome:
        q = q.where(DeliveryImportRow.outcome == outcome)
    return _page(db, q.order_by(DeliveryImportRow.row_number), limit, offset, lambda r: {"row_number": r.row_number, "outcome": r.outcome.value, "errors": r.errors, "raw": r.raw, "record_id": r.record_id})  # fmt: skip


# ------------------------------------------------------------------------------------------------ deliveries


def _load(db, request: Request, as_of: datetime, supplier_id, destination_id, product_id, refs=None):  # noqa: ANN001, ANN202
    lim = _limits(request)
    ds = load_deliveries(db, as_of=as_of, supplier_id=supplier_id, destination_id=destination_id, product_id=product_id, refs=refs, limit=lim.max_records + 1)  # fmt: skip
    if len(ds) > lim.max_records:
        raise DomainValidationError(
            f"The scope holds more than {lim.max_records} order lines; narrow it by supplier, destination or product."
        )
    return ds


@router.get("/deliveries", summary="Deliveries as of a time (server-side filtering and pagination)")
def list_deliveries(
    request: Request, db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0, supplier_id: Ident = None, destination_facility_id: Ident = None,
    product_id: Ident = None, as_of: datetime | None = None, state: Annotated[str | None, Query(max_length=20)] = None,
    q: Annotated[str | None, Query(max_length=128)] = None,
) -> dict[str, Any]:  # fmt: skip
    at = _as_of(as_of)
    ds = _load(db, request, at, supplier_id, destination_facility_id, product_id)
    rows = []
    for d in sorted(ds, key=lambda x: (x.order_at or at, x.ref)):
        st, why = sem.classify(d, at, 0.0)
        if state and st != state:
            continue
        if q and q.lower() not in d.ref.lower():
            continue
        rows.append({**summary_of(d), "state": st, "state_reason": why})
    return {
        "items": rows[offset : offset + limit],
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "as_of": at.isoformat(),
    }


@router.get("/deliveries/{delivery_ref}", responses=ERR, summary="One delivery's resolved state, snapshots and events")
def get_delivery(delivery_ref: Annotated[str, Path(min_length=1, max_length=128)], product_id: Annotated[int, Query(ge=1, le=2**31 - 1)], request: Request, db: DbSession, _a: Viewer, as_of: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    at = _as_of(as_of)
    d = next(
        (x for x in _load(db, request, at, None, None, product_id, refs=[delivery_ref]) if x.ref == delivery_ref), None
    )
    if d is None:
        raise NotFoundError("No delivery with this reference and product was known at as_of.")
    st, why = sem.classify(d, at, 0.0)
    snaps = list(db.scalars(select(DeliverySnapshot).where(DeliverySnapshot.delivery_ref == delivery_ref, DeliverySnapshot.product_id == product_id).order_by(DeliverySnapshot.observed_at, DeliverySnapshot.id)))  # fmt: skip
    evs = list(
        db.scalars(
            select(DeliveryEvent)
            .where(DeliveryEvent.delivery_ref == delivery_ref)
            .order_by(DeliveryEvent.occurred_at, DeliveryEvent.id)
        )
    )
    return {
        **summary_of(d), "state": st, "state_reason": why, "as_of": at.isoformat(),
        "deviation_hours": sem.deviation_hours(d, "ORIGINAL"), "snapshots_detail": [{"id": s.id, "observed_at": _iso(s.observed_at), "batch_id": s.batch_id, "reported_status": s.reported_status} for s in snaps],  # fmt: skip
        "events_detail": [{"id": e.id, "event_type": e.event_type, "occurred_at": _iso(e.occurred_at), "value_at": _iso(e.value_at), "quantity": e.quantity} for e in evs],  # fmt: skip
        "statements": [STATEMENTS["evidence"]],
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ analytics


def _acfg(
    definition, unit, commitment, grace_hours, timezone_, start_event, supplier_id, destination_facility_id, product_id
) -> LeadTimeConfig:  # noqa: ANN001
    return _cfg_from_query(definition=definition, unit=unit, commitment=commitment, grace_hours=grace_hours, timezone=timezone_, start_event=start_event, supplier_id=supplier_id, destination_facility_id=destination_facility_id, product_id=product_id)  # fmt: skip


Def = Annotated[str | None, Query(max_length=24)]
Unit = Annotated[Literal["HOURS", "CALENDAR_DAYS", "BUSINESS_DAYS"] | None, Query()]
Cmt = Annotated[Literal["ORIGINAL", "LATEST"] | None, Query()]
Grace = Annotated[float | None, Query(ge=0, le=720)]
Tz = Annotated[str | None, Query(max_length=64)]
Start = Annotated[Literal["ORDER", "CONFIRMATION"] | None, Query()]


@router.get("/analytics/summary", summary="Lead-time and punctuality statistics with separate denominators")
def summary(request: Request, db: DbSession, _a: Viewer, definition: Def = None, unit: Unit = None, commitment: Cmt = None, grace_hours: Grace = None, timezone: Tz = None, start_event: Start = None, supplier_id: Ident = None, destination_facility_id: Ident = None, product_id: Ident = None, as_of: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    cfg = _acfg(
        definition,
        unit,
        commitment,
        grace_hours,
        timezone,
        start_event,
        supplier_id,
        destination_facility_id,
        product_id,
    )
    at = _as_of(as_of)
    out = analytics.summarize(_load(db, request, at, supplier_id, destination_facility_id, product_id), cfg, at)
    out["statements"] = [STATEMENTS["evidence"]]
    return out


@router.get("/analytics/groups", summary="Grouped statistics (no ranking from sparse groups)")
def groups(request: Request, db: DbSession, _a: Viewer, by: Annotated[str, Query(max_length=16)] = "supplier", definition: Def = None, unit: Unit = None, commitment: Cmt = None, grace_hours: Grace = None, timezone: Tz = None, start_event: Start = None, supplier_id: Ident = None, destination_facility_id: Ident = None, product_id: Ident = None, as_of: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    if by not in analytics.GROUPS:
        raise DomainValidationError(f"by must be one of {', '.join(analytics.GROUPS)}.")
    cfg = _acfg(
        definition,
        unit,
        commitment,
        grace_hours,
        timezone,
        start_event,
        supplier_id,
        destination_facility_id,
        product_id,
    )
    at = _as_of(as_of)
    out = analytics.group_summary(_load(db, request, at, supplier_id, destination_facility_id, product_id), cfg, at, by)
    out["statements"] = [STATEMENTS["evidence"]]
    return out


@router.get("/analytics/trend", summary="Recent window versus earlier history (non-overlapping Wilson intervals only)")
def trend(request: Request, db: DbSession, _a: Viewer, window_days: Annotated[int, Query(ge=7, le=730)] = 90, definition: Def = None, unit: Unit = None, commitment: Cmt = None, grace_hours: Grace = None, timezone: Tz = None, start_event: Start = None, supplier_id: Ident = None, destination_facility_id: Ident = None, product_id: Ident = None, as_of: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    cfg = _acfg(
        definition,
        unit,
        commitment,
        grace_hours,
        timezone,
        start_event,
        supplier_id,
        destination_facility_id,
        product_id,
    )
    at = _as_of(as_of)
    out = analytics.recent_vs_history(
        _load(db, request, at, supplier_id, destination_facility_id, product_id), cfg, at, window_days
    )
    out["statements"] = [STATEMENTS["evidence"]]
    return out


@router.get("/quality", summary="Data-quality findings over the delivery records in scope")
def quality(request: Request, db: DbSession, _a: Viewer, supplier_id: Ident = None, destination_facility_id: Ident = None, product_id: Ident = None, as_of: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    at = _as_of(as_of)
    ds = _load(db, request, at, supplier_id, destination_facility_id, product_id)
    p = analytics.summarize(ds, LeadTimeConfig(), at)
    flags: dict[str, int] = {}
    for d in ds:
        for f in d.flags or []:
            flags[str(f)] = flags.get(str(f), 0) + 1
    findings = []
    c = p["counts"]
    if c["completed_missing_timestamps_for_definition"]:
        findings.append({"code": "MISSING_TIMESTAMPS", "severity": "WARNING", "count": c["completed_missing_timestamps_for_definition"], "text": "Completed deliveries lack a timestamp the lead-time definition needs; they are excluded, not imputed."})  # fmt: skip
    if p["coverage"]["share_with_commitment"] is not None and p["coverage"]["share_with_commitment"] < 1:
        findings.append({"code": "MISSING_PROMISE", "severity": "WARNING", "count": None, "text": "Some completed deliveries have no promised date; they are unknown for punctuality, never counted as on time."})  # fmt: skip
    for k, n in flags.items():
        findings.append(
            {
                "code": k,
                "severity": "INFO",
                "count": n,
                "text": "Flag raised while resolving records (correction, conflict or exception).",
            }
        )
    findings += [{"code": "WARNING", "severity": "INFO", "count": None, "text": w} for w in p["warnings"]]
    return {
        "as_of": at.isoformat(),
        "deliveries": len(ds),
        "counts": c,
        "flags": flags,
        "findings": findings,
        "freshness": p["freshness"],
    }


@router.get(
    "/suppliers/{supplier_id}/reliability",
    responses=ERR,
    summary="Delivery reliability evidence for a supplier, beside (not inside) the supplier-risk score",
)
def supplier_reliability(supplier_id: IdPath, request: Request, db: DbSession, _a: Viewer, as_of: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    at = _as_of(as_of)
    ds = _load(db, request, at, supplier_id, None, None)
    cfg = LeadTimeConfig(supplier_id=supplier_id)
    last = db.scalar(select(RiskAssessment).where(RiskAssessment.supplier_id == supplier_id).order_by(RiskAssessment.computed_at.desc(), RiskAssessment.id.desc()))  # fmt: skip
    factor = None
    if last is not None:
        f = db.scalar(
            select(RiskFactor).where(RiskFactor.assessment_id == last.id, RiskFactor.category == RiskCategory.DELIVERY)
        )
        if f is not None:
            factor = {"assessment_id": last.id, "assessment_uid": last.assessment_uid, "computed_at": _iso(last.computed_at), "state": f.state.value, "score": f.score, "explanation": f.explanation, "unavailable_reason": f.unavailable_reason}  # fmt: skip
    return {
        "supplier_id": supplier_id, "as_of": at.isoformat(), "summary": analytics.summarize(ds, cfg, at), "supplier_risk_delivery_factor": factor,
        "note": "The supplier-risk delivery factor above is a separate, previously stored assessment; it is shown for context and is neither changed nor recomputed here.",  # fmt: skip
        "statements": [STATEMENTS["evidence"], STATEMENTS["decision"]],
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ runs


@router.post("/runs", status_code=202, responses=ERR, summary="Queue a training/evaluation run (idempotent with a key)")
def create_run(
    body: dict[str, Any], tasks: BackgroundTasks, response: Response, db: DbSession, request: Request, actor: Editor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")] = None,
) -> dict[str, Any]:  # fmt: skip
    _writes(request)
    conf = _conf(body)
    lim = _limits(request)
    with db.begin():
        r, created = runs.create_run(db, conf, actor.name, lim, idempotency_key=idempotency_key)
        if created:
            audit.record(db, actor, "leadtime.run.queued", target_type="LEADTIME_RUN", target_id=r.id, details={"target": r.target, "config_hash": r.config_hash}, request_id=request_id_of(request))  # fmt: skip
        out = _run(r)
    if created:
        tasks.add_task(runs.execute_run, request_session_factory(request), out["id"], lim)
    else:
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.get("/runs", summary="Runs (newest first)")
def list_runs(db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0, target: Literal["LEAD_TIME", "LATE_RISK"] | None = None, status: SimStatus | None = None, model: Annotated[str | None, Query(max_length=40)] = None) -> dict[str, Any]:  # fmt: skip
    q = select(LeadTimeRun)
    for col, v in ((LeadTimeRun.target, target), (LeadTimeRun.status, status), (LeadTimeRun.selected_model, model)):
        if v is not None:
            q = q.where(col == v)
    return _page(db, q.order_by(LeadTimeRun.id.desc()), limit, offset, _run)


@router.get("/runs/{run_id}", responses=ERR, summary="One run with configuration, selection and evidence")
def get_run(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _run(_get(db, LeadTimeRun, run_id, "Run"), full=True)


@router.post("/runs/{run_id}/cancel", responses=ERR, summary="Cancel a queued run, or ask a running one to stop")
def cancel(run_id: IdPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        r = _get(db, LeadTimeRun, run_id, "Run")
        if r.requested_by != actor.name and actor.role < Role.REVIEWER:
            raise ForbiddenError("Only the requester or a reviewer can cancel this run.")
        runs.cancel_run(db, r)
        audit.record(db, actor, "leadtime.run.cancel", target_type="LEADTIME_RUN", target_id=r.id, details={"status": r.status.value}, request_id=request_id_of(request))  # fmt: skip
        return _run(r)


def _section(run_id: int, db, names: dict[str, str], extra: dict[str, Any] | None = None) -> dict[str, Any]:  # noqa: ANN001
    r = _finished(_get(db, LeadTimeRun, run_id, "Run"))
    return {"run": _run(r), **{k: getattr(r, attr) for k, attr in names.items()}, **(extra or {}), "statements": [STATEMENTS["evidence"], STATEMENTS["synthetic"]] if r.provenance == "SYNTHETIC" else [STATEMENTS["evidence"]]}  # fmt: skip


@router.get(
    "/runs/{run_id}/metrics",
    responses=ERR,
    summary="Per-model validation/test metrics, selection and baseline comparison",
)
def run_metrics(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _section(run_id, db, {"evaluation": "evaluation", "selection": "selection", "candidates": "candidates"})


@router.get(
    "/runs/{run_id}/splits", responses=ERR, summary="Chronological split boundaries, purging and leakage controls"
)
def run_splits(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _section(
        run_id, db, {"splits": "splits", "leakage_controls": "leakage_controls", "preparation": "preparation"}
    )


@router.get("/runs/{run_id}/calibration", responses=ERR, summary="Calibration decision and evidence (risk runs)")
def run_calibration(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _section(run_id, db, {"calibration": "calibration"})


@router.get(
    "/runs/{run_id}/intervals", responses=ERR, summary="Interval method and empirical coverage (lead-time runs)"
)
def run_intervals(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _section(run_id, db, {"intervals": "intervals"})


@router.get(
    "/runs/{run_id}/explanation",
    responses=ERR,
    summary="Coefficients and permutation importance (association, not causation)",
)
def run_explanation(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _section(
        run_id,
        db,
        {"explanation": "explanation"},
        {"note": "Importance describes what the fitted model uses; it is not evidence of cause."},
    )


@router.get("/runs/{run_id}/failures", responses=ERR, summary="Run error, model failures and warnings")
def run_failures(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _get(db, LeadTimeRun, run_id, "Run")
    return {"run": _run(r), "error": r.error, "failures": r.failures or [], "warnings": r.warnings or []}


# ------------------------------------------------------------------------------------------------ assessments


@router.post(
    "/assessments",
    status_code=201,
    responses=ERR,
    summary="Issue a persisted delivery or lane assessment (idempotent with a key)",
)
def create_assessment(
    body: AssessRequest, response: Response, db: DbSession, request: Request, actor: Editor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")] = None,
) -> dict[str, Any]:  # fmt: skip
    _writes(request)
    with db.begin():
        a, created = assess(db, body, actor.name, idempotency_key=idempotency_key)
        if created:
            audit.record(db, actor, "leadtime.assessment", target_type="DELIVERY_ASSESSMENT", target_id=a.id, details={"subject": a.subject, "status": a.status, "delivery_ref": a.delivery_ref}, request_id=request_id_of(request))  # fmt: skip
        out = _assessment(a, full=True)
    if not created:
        response.status_code = 200
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.get("/assessments", summary="Issued assessments (newest first)")
def list_assessments(db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0, supplier_id: Ident = None, product_id: Ident = None, delivery_ref: Annotated[str | None, Query(max_length=128)] = None, status: Annotated[str | None, Query(max_length=24)] = None) -> dict[str, Any]:  # fmt: skip
    q = select(DeliveryAssessment)
    for col, v in ((DeliveryAssessment.supplier_id, supplier_id), (DeliveryAssessment.product_id, product_id), (DeliveryAssessment.delivery_ref, delivery_ref), (DeliveryAssessment.status, status)):  # fmt: skip
        if v is not None:
            q = q.where(col == v)
    return _page(db, q.order_by(DeliveryAssessment.id.desc()), limit, offset, _assessment)


@router.get("/assessments/{assessment_id}", responses=ERR, summary="One assessment with factors, history and evidence")
def get_assessment(assessment_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _assessment(_get(db, DeliveryAssessment, assessment_id, "Assessment"), full=True)


@router.get(
    "/assessments/{assessment_id}/outcome",
    responses=ERR,
    summary="Compare an issued assessment with the eventual outcome (the assessment is not edited)",
)
def assessment_outcome(assessment_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return outcome_of(db, _get(db, DeliveryAssessment, assessment_id, "Assessment"))


__all__ = ["router"]
