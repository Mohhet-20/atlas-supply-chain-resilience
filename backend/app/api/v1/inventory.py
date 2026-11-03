"""Inventory & Stockout Intelligence API.

Imports write only ``inventory_*``/``demand_*`` tables; analyses write only ``stockout_*`` tables (and audit). Nothing here
creates or edits registry records, orders or relationships. Data is limited to the caller's organisation, identity is the
authenticated actor only and request bodies reject unknown fields (including any client-supplied organisation id).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from app.api.v1.deps import DbSession
from app.api.v1.ingestion import _format_of, _read_body, _require_write
from app.core.auth import Actor, Role, require
from app.core.errors import ConflictError, DomainValidationError, ForbiddenError, NotFoundError, WriteDisabledError
from app.db.tenancy import request_session_factory
from app.ingestion.constants import ALLOWED_DATE_FORMATS
from app.inventory import calc, ingest, runs
from app.inventory import config as cfg
from app.inventory.analysis import window_demand
from app.models.enums import Provenance, SimStatus, SourceFormat
from app.models.facility import Facility
from app.models.inventory import (
    AnalysisKind,
    DemandKind,
    DemandObservation,
    ImportKind,
    InventoryImportBatch,
    InventoryImportRow,
    InventoryPolicy,
    InventoryPosition,
    InventoryReplenishment,
    InvRowOutcome,
    PolicyStatus,
    ReceiptStatus,
    StockoutAnalysis,
    StockoutFinding,
)
from app.models.product import Product
from app.models.types import utcnow
from app.registry import audit
from app.registry.audit import request_id_of
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/inventory", tags=["inventory & stockout intelligence"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 413, 415, 422)}
IdPath = Annotated[int, Path(ge=1, le=2**31 - 1)]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Lim = Annotated[int, Query(ge=1, le=200)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]
Ident = Annotated[int | None, Query(ge=1, le=2**31 - 1)]


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError(
            "Inventory write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED)."
        )


def _limits(request: Request) -> runs.Limits:
    return runs.limits_from(request.app.state.settings)


def _page(db, q, limit, offset, shape) -> dict[str, Any]:  # noqa: ANN001
    total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0
    return {
        "items": [shape(r) for r in db.scalars(q.limit(limit).offset(offset))],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _iso(d: Any) -> str | None:
    return None if d is None else d.isoformat()


# ----------------------------------------------------------------------------------------------------------- shapes


def _batch(b: InventoryImportBatch) -> dict[str, Any]:
    return {
        "id": b.id, "batch_uid": b.batch_uid, "kind": b.kind.value, "status": b.status.value, "source_label": b.source_label,
        "provenance": b.provenance.value, "file_name": b.file_name, "file_format": b.file_format, "requested_by": b.requested_by,
        "mapping": b.mapping, "rows_total": b.rows_total, "rows_accepted": b.rows_accepted, "rows_duplicate": b.rows_duplicate,
        "rows_rejected": b.rows_rejected, "summary": b.summary, "created_at": _iso(b.created_at),
    }  # fmt: skip


def _position(p: InventoryPosition, names: dict[str, dict[int, str]] | None = None) -> dict[str, Any]:
    out = {
        "id": p.id, "product_id": p.product_id, "facility_id": p.facility_id, "observed_at": _iso(p.observed_at), "unit": p.unit,
        "on_hand": p.on_hand, "reserved": p.reserved, "allocated": p.allocated, "quarantined": p.quarantined, "damaged": p.damaged,
        "usable_source": p.usable_source, "atp_source": p.atp_source, "in_transit": p.in_transit, "backordered": p.backordered,
        "provenance": p.provenance.value, "source_label": p.source_label, "batch_id": p.batch_id, "source_record_id": p.source_record_id,
    }  # fmt: skip
    if names:
        out["product_name"], out["facility_name"] = names["p"].get(p.product_id), names["f"].get(p.facility_id)
    return out


def _policy(p: InventoryPolicy) -> dict[str, Any]:
    return {
        "id": p.id, "product_id": p.product_id, "facility_id": p.facility_id, "version": p.version, "status": p.status.value, "unit": p.unit,
        "safety_stock": p.safety_stock, "reorder_point": p.reorder_point, "minimum": p.minimum, "maximum": p.maximum, "target": p.target,
        "review_period_days": p.review_period_days, "lead_time_days": p.lead_time_days, "lead_time_input": p.lead_time_input,
        "service_level": p.service_level, "effective_from": _iso(p.effective_from), "effective_to": _iso(p.effective_to), "owner": p.owner,
        "provenance": p.provenance.value, "note": p.note, "created_by": p.created_by, "created_at": _iso(p.created_at),
    }  # fmt: skip


def _analysis(a: StockoutAnalysis, full: bool = False) -> dict[str, Any]:
    out = {
        "id": a.id, "analysis_uid": a.analysis_uid, "kind": a.kind.value, "status": a.status.value, "requested_by": a.requested_by,
        "impact_analysis_id": a.impact_analysis_id, "config": a.config, "config_hash": a.config_hash, "algorithm_version": a.algorithm_version,
        "as_of": _iso(a.as_of), "input_hash": a.input_hash, "queued_at": _iso(a.queued_at), "started_at": _iso(a.started_at),
        "finished_at": _iso(a.finished_at), "duration_ms": a.duration_ms, "truncated": a.truncated, "truncation_reasons": a.truncation_reasons,
        "error": a.error, "cancel_requested": a.cancel_requested, "disclaimer": cfg.DISCLAIMER,
    }  # fmt: skip
    if full:
        out |= {
            "summary": a.summary,
            "coverage": a.coverage,
            "inputs": a.inputs,
            "warnings": a.warnings,
            "failures": a.failures,
        }
    else:
        out["summary"] = (
            None if a.summary is None else {k: a.summary.get(k) for k in ("pairs_analysed", "by_status", "kind")}
        )
    return out


def _finding(f: StockoutFinding, full: bool = False) -> dict[str, Any]:
    out = {
        "id": f.id, "analysis_id": f.analysis_id, "product_id": f.product_id, "facility_id": f.facility_id, "product_code": f.product_code,
        "product_name": f.product_name, "facility_code": f.facility_code, "facility_name": f.facility_name, "unit": f.unit, "status": f.status,
        "severity_rank": f.severity_rank, "reason": f.reason, "change": f.change, "confidence": f.confidence, "coverage": f.coverage,
        "days_of_inventory": (f.coverage_days or {}).get("days"), "days_of_inventory_status": (f.coverage_days or {}).get("status"),
        "stockout_within_day": (f.projection or {}).get("stockout_within_day"), "explanation": f.explanation,
    }  # fmt: skip
    if full:
        out |= {
            "quantities": f.quantities, "demand": f.demand, "coverage_days": f.coverage_days, "policy": f.policy, "projection": f.projection,
            "scenario": f.scenario, "confidence_basis": f.confidence_basis, "coverage_detail": f.coverage_detail,
            "data_quality": f.data_quality, "assumptions": f.assumptions, "evidence": f.evidence, "disclaimer": cfg.DISCLAIMER,
        }  # fmt: skip
    return out


# ------------------------------------------------------------------------------------------------------------- misc


@router.get("/limits", summary="Server limits and the fields each import kind accepts")
def limits(request: Request, _a: Viewer) -> dict[str, Any]:
    s = request.app.state.settings
    return {
        "max_upload_bytes": s.max_upload_bytes, "max_import_rows": s.inventory_max_import_rows, "max_pairs": s.inventory_max_pairs,
        "max_horizon_days": s.inventory_max_horizon_days, "time_budget_seconds": s.inventory_time_budget_seconds,
        "max_active_analyses": s.inventory_max_active_analyses, "date_formats": list(ALLOWED_DATE_FORMATS),
        "fields": {k.value: {"required": list(ingest.REQUIRED[k]), "optional": list(ingest.OPTIONAL[k])} for k in ImportKind},
        "demand_kinds": [k.value for k in DemandKind], "receipt_statuses": [k.value for k in ReceiptStatus],
        "algorithm_version": cfg.ALGORITHM_VERSION, "disclaimer": cfg.DISCLAIMER,
    }  # fmt: skip


# ----------------------------------------------------------------------------------------------------------- imports


def _mapping(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    try:
        m = json.loads(raw)
    except ValueError as exc:
        raise DomainValidationError("mapping must be a JSON object of field -> column.") from exc
    if (
        not isinstance(m, dict)
        or len(m) > 40
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in m.items())
    ):
        raise DomainValidationError("mapping must be a JSON object of field -> column.")
    return m


@router.post(
    "/imports", status_code=200, responses=ERR,
    summary="Preview (mode=preview, nothing written) or commit (mode=commit) an inventory, demand or replenishment file",
)  # fmt: skip
async def import_file(
    request: Request, db: DbSession, actor: Editor, kind: ImportKind, mode: Literal["preview", "commit"] = "preview",
    format: Annotated[Literal["CSV", "JSON"] | None, Query()] = None, filename: Annotated[str | None, Query(max_length=255)] = None,
    mapping: Annotated[str | None, Query(max_length=4000, description="JSON object: canonical field -> file column.")] = None,
    source_label: Annotated[str, Query(min_length=1, max_length=200)] = "uploaded file",
    provenance: Provenance = Provenance.USER_PROVIDED, atomic: bool = False, date_format: str = "%Y-%m-%d",
    delimiter: Annotated[str, Query(max_length=8)] = ",", encoding: Annotated[str | None, Query(max_length=20)] = None,
) -> dict[str, Any]:  # fmt: skip
    if mode == "commit":
        _require_write(request)
    data = await _read_body(request)
    if not data:
        raise DomainValidationError("The request body is empty. Send the file bytes as the body.")
    fmt = _format_of(request, SourceFormat(format) if format else None, filename)
    kw = dict(
        fmt=fmt, encoding=encoding, delimiter=delimiter, mapping=_mapping(mapping), date_format=date_format,
        max_rows=request.app.state.settings.inventory_max_import_rows,
    )  # fmt: skip
    if mode == "preview":
        return ingest.preview(db, kind, data, **kw)
    with db.begin():
        batch, created, prep = ingest.commit(
            db, kind, data, source_label=source_label, provenance=provenance, requested_by=actor.name, file_name=filename,
            atomic=atomic, **kw,
        )  # fmt: skip
        if created:
            audit.record(
                db, actor, "inventory.import", target_type="INVENTORY_IMPORT", target_id=batch.id,
                details={"kind": kind.value, "accepted": batch.rows_accepted, "rejected": batch.rows_rejected, "duplicate": batch.rows_duplicate},
                request_id=request_id_of(request),
            )  # fmt: skip
        out = _batch(batch)
    return {
        "mode": "commit",
        "committed": out["rows_accepted"] > 0,
        "created": created,
        "replayed_existing_batch": not created,
        "batch": out,
    }


@router.get("/imports", summary="Import batches (newest first)")
def list_imports(
    db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0, kind: ImportKind | None = None
) -> dict[str, Any]:
    q = select(InventoryImportBatch)
    if kind:
        q = q.where(InventoryImportBatch.kind == kind)
    return _page(db, q.order_by(InventoryImportBatch.id.desc()), limit, offset, _batch)


def _get(db, model, pk: int, what: str):  # noqa: ANN001, ANN202
    r = db.get(model, pk)
    if r is None:
        raise NotFoundError(f"{what} not found.")
    return r


@router.get("/imports/{batch_id}", responses=ERR, summary="One import batch")
def get_import(batch_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _batch(_get(db, InventoryImportBatch, batch_id, "Import batch"))


@router.get("/imports/{batch_id}/rows", responses=ERR, summary="Row-level outcomes and errors for a batch")
def import_rows(
    batch_id: IdPath, db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0, outcome: InvRowOutcome | None = None,
) -> dict[str, Any]:  # fmt: skip
    _get(db, InventoryImportBatch, batch_id, "Import batch")
    q = select(InventoryImportRow).where(InventoryImportRow.batch_id == batch_id)
    if outcome:
        q = q.where(InventoryImportRow.outcome == outcome)
    return _page(
        db, q.order_by(InventoryImportRow.row_number), limit, offset,
        lambda r: {"row_number": r.row_number, "outcome": r.outcome.value, "errors": r.errors, "raw": r.raw, "record_id": r.record_id},
    )  # fmt: skip


# --------------------------------------------------------------------------------------------------------- positions


def _names(db, rows) -> dict[str, dict[int, str]]:  # noqa: ANN001
    pids, fids = {r.product_id for r in rows}, {r.facility_id for r in rows}
    return {
        "p": dict(db.execute(select(Product.id, Product.name).where(Product.id.in_(pids))).all()) if pids else {},
        "f": dict(db.execute(select(Facility.id, Facility.name).where(Facility.id.in_(fids))).all()) if fids else {},
    }


@router.get(
    "/positions", summary="Inventory snapshots; latest=true returns only the newest snapshot per product-facility"
)
def positions(
    db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0, product_id: Ident = None, facility_id: Ident = None, latest: bool = True,
    observed_before: datetime | None = None,
) -> dict[str, Any]:  # fmt: skip
    q = select(InventoryPosition)
    if product_id:
        q = q.where(InventoryPosition.product_id == product_id)
    if facility_id:
        q = q.where(InventoryPosition.facility_id == facility_id)
    if observed_before:
        q = q.where(InventoryPosition.observed_at <= observed_before)
    if latest:
        sub = (
            select(func.max(InventoryPosition.observed_at).label("t"), InventoryPosition.product_id.label("p"), InventoryPosition.facility_id.label("f"))
            .group_by(InventoryPosition.product_id, InventoryPosition.facility_id)
            .subquery()
        )  # fmt: skip
        if observed_before:
            sub = (
                select(func.max(InventoryPosition.observed_at).label("t"), InventoryPosition.product_id.label("p"), InventoryPosition.facility_id.label("f"))
                .where(InventoryPosition.observed_at <= observed_before).group_by(InventoryPosition.product_id, InventoryPosition.facility_id).subquery()
            )  # fmt: skip
        q = q.join(
            sub,
            (InventoryPosition.product_id == sub.c.p)
            & (InventoryPosition.facility_id == sub.c.f)
            & (InventoryPosition.observed_at == sub.c.t),
        )
    q = q.order_by(InventoryPosition.observed_at.desc(), InventoryPosition.id.desc())
    total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0
    rows = list(db.scalars(q.limit(limit).offset(offset)))
    names = _names(db, rows)
    return {
        "items": [_position(r, names) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "latest_only": latest,
    }


@router.get(
    "/positions/{position_id}", responses=ERR, summary="One snapshot with its derived quantities (formulas shown)"
)
def position_detail(position_id: IdPath, db: DbSession, _a: Viewer, max_age_days: Annotated[int, Query(ge=1, le=365)] = 7,
                    allocated_overlap: Literal["UNKNOWN", "DISTINCT", "INCLUDED_IN_RESERVED"] = "UNKNOWN") -> dict[str, Any]:  # fmt: skip
    p = _get(db, InventoryPosition, position_id, "Snapshot")
    prod = db.get(Product, p.product_id)
    pos = calc.Position(
        p.id, p.product_id, p.facility_id, p.unit, p.observed_at, p.on_hand, p.reserved, p.allocated, p.quarantined,
        p.damaged, p.usable_source, p.atp_source, p.in_transit, p.backordered,
    )  # fmt: skip
    q = calc.derive_quantities(
        pos,
        product_unit=prod.unit_of_measure if prod else None,
        as_of=utcnow(),
        max_age_days=max_age_days,
        allocated_overlap=allocated_overlap,
    )
    return {
        "snapshot": _position(p, _names(db, [p])),
        "derived": q,
        "note": "Derived against the current time; an analysis stores the values it used.",
    }


@router.get("/history", summary="All snapshots for one product at one facility, oldest first")
def position_history(db: DbSession, _a: Viewer, product_id: Annotated[int, Query(ge=1, le=2**31 - 1)], facility_id: Annotated[int, Query(ge=1, le=2**31 - 1)],
                     limit: Lim = 100, offset: Off = 0) -> dict[str, Any]:  # fmt: skip
    q = (
        select(InventoryPosition)
        .where(InventoryPosition.product_id == product_id, InventoryPosition.facility_id == facility_id)
        .order_by(InventoryPosition.observed_at, InventoryPosition.id)
    )
    return _page(db, q, limit, offset, _position)


@router.get("/quality", summary="Inventory data-quality overview (inventory-specific; not the registry quality score)")
def quality(db: DbSession, _a: Viewer, max_age_days: Annotated[int, Query(ge=1, le=365)] = 7) -> dict[str, Any]:
    now = utcnow()
    latest = db.execute(
        select(
            InventoryPosition.product_id, InventoryPosition.facility_id, func.max(InventoryPosition.observed_at)
        ).group_by(InventoryPosition.product_id, InventoryPosition.facility_id)
    ).all()
    stale = [
        (p, f)
        for p, f, t in latest
        if (now - (t if t.tzinfo else t.replace(tzinfo=now.tzinfo))) > timedelta(days=max_age_days)
    ]
    dem_pairs = set(db.execute(select(DemandObservation.product_id, DemandObservation.facility_id).distinct()).all())
    pol_pairs = set(
        db.execute(
            select(InventoryPolicy.product_id, InventoryPolicy.facility_id).where(
                InventoryPolicy.status == PolicyStatus.ACTIVE
            )
        ).all()
    )
    pairs = {(p, f) for p, f, _ in latest}
    outcomes = dict(
        db.execute(select(InventoryImportRow.outcome, func.count()).group_by(InventoryImportRow.outcome)).all()
    )
    codes: dict[str, int] = {}
    for errs in db.scalars(
        select(InventoryImportRow.errors).where(InventoryImportRow.outcome == InvRowOutcome.REJECTED).limit(5000)
    ):
        for e in errs or []:
            codes[e.get("code", "?")] = codes.get(e.get("code", "?"), 0) + 1
    return {
        "pairs_with_inventory": len(pairs), "stale_pairs": len(stale), "max_age_days": max_age_days,
        "pairs_without_demand": len(pairs - dem_pairs), "pairs_without_policy": len(pairs - pol_pairs),
        "import_rows_by_outcome": {k.value: v for k, v in outcomes.items()}, "top_rejection_codes": sorted(codes.items(), key=lambda kv: -kv[1])[:10],
        "note": "These counts are inventory-specific. They are not yet recorded as rows in the registry data-quality findings table.",
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ demand and receipts


@router.get("/demand", summary="Demand observations")
def demand(
    db: DbSession,
    _a: Viewer,
    limit: Lim = 50,
    offset: Off = 0,
    product_id: Ident = None,
    facility_id: Ident = None,
    kind: DemandKind | None = None,
) -> dict[str, Any]:
    q = select(DemandObservation)
    for col, v in (
        (DemandObservation.product_id, product_id),
        (DemandObservation.facility_id, facility_id),
        (DemandObservation.kind, kind),
    ):
        if v:
            q = q.where(col == v)
    return _page(
        db, q.order_by(DemandObservation.period_start.desc(), DemandObservation.id.desc()), limit, offset,
        lambda d: {"id": d.id, "product_id": d.product_id, "facility_id": d.facility_id, "kind": d.kind.value, "period_start": _iso(d.period_start), "period_end": _iso(d.period_end), "quantity": d.quantity, "unit": d.unit, "provenance": d.provenance.value, "batch_id": d.batch_id},
    )  # fmt: skip


@router.get("/demand/rate", responses=ERR, summary="Transparent observed demand rate for one product at one facility")
def demand_rate(
    db: DbSession, _a: Viewer, product_id: Annotated[int, Query(ge=1, le=2**31 - 1)], facility_id: Annotated[int, Query(ge=1, le=2**31 - 1)],
    window_days: Annotated[int, Query(ge=7, le=365)] = 30, basis: Literal["AUTO", "CONSUMPTION", "DEMAND", "SALES"] = "AUTO",
    as_of: datetime | None = None,
) -> dict[str, Any]:  # fmt: skip
    prod = _get(db, Product, product_id, "Product")
    _get(db, Facility, facility_id, "Facility")
    at = as_of or utcnow()
    conf = cfg.InventoryConfig(
        as_of=at,
        demand_window_days=window_days,
        demand_basis=basis,
        product_ids=[product_id],
        facility_ids=[facility_id],
    )
    rows = window_demand(db, conf, at.date()).get((product_id, facility_id), [])
    obs = [calc.DemandObs(d.id, d.period_start, d.period_end, d.quantity, d.unit, d.kind.value) for d in rows]
    out = calc.demand_rate(
        obs,
        unit=prod.unit_of_measure,
        as_of_date=at.date(),
        window_days=window_days,
        basis=basis,
        min_obs=conf.min_demand_observations,
        min_coverage=conf.min_demand_coverage,
    )
    return out | {
        "note": "Observed history only. This is not a forecast and missing periods are not treated as zero demand."
    }


@router.get("/replenishments", summary="Recorded inbound receipts (latest version of each reference)")
def replenishments(
    db: DbSession,
    _a: Viewer,
    limit: Lim = 50,
    offset: Off = 0,
    product_id: Ident = None,
    facility_id: Ident = None,
    status: ReceiptStatus | None = None,
) -> dict[str, Any]:
    q = select(InventoryReplenishment)
    for col, v in (
        (InventoryReplenishment.product_id, product_id),
        (InventoryReplenishment.facility_id, facility_id),
        (InventoryReplenishment.status, status),
    ):
        if v:
            q = q.where(col == v)
    return _page(
        db, q.order_by(InventoryReplenishment.expected_date, InventoryReplenishment.id), limit, offset,
        lambda r: {"id": r.id, "product_id": r.product_id, "facility_id": r.facility_id, "supplier_id": r.supplier_id, "reference": r.reference, "status": r.status.value, "quantity": r.quantity, "unit": r.unit, "expected_date": _iso(r.expected_date), "observed_at": _iso(r.observed_at)},
    )  # fmt: skip


# ----------------------------------------------------------------------------------------------------------- policies


class LeadTime(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float = Field(ge=0, le=3650)
    unit: Literal["hours", "days", "weeks"]


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit: str = Field(min_length=1, max_length=32)
    safety_stock: float | None = Field(default=None, ge=0, le=1e12)
    reorder_point: float | None = Field(default=None, ge=0, le=1e12)
    minimum: float | None = Field(default=None, ge=0, le=1e12)
    maximum: float | None = Field(default=None, ge=0, le=1e12)
    target: float | None = Field(default=None, ge=0, le=1e12)
    review_period_days: float | None = Field(default=None, ge=0, le=3650)
    lead_time: LeadTime | None = None
    service_level: float | None = Field(default=None, ge=0.5, lt=1)
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    owner: str = Field(min_length=1, max_length=64)
    provenance: Provenance = Provenance.USER_PROVIDED
    note: str | None = Field(default=None, max_length=400)

    @model_validator(mode="after")
    def _window(self) -> PolicyBody:
        if self.effective_from and self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class PolicyCreate(PolicyBody):
    product_id: int = Field(ge=1, le=2**31 - 1)
    facility_id: int = Field(ge=1, le=2**31 - 1)


def _new_policy(db, product_id: int, facility_id: int, body: PolicyBody, version: int, actor: Actor) -> InventoryPolicy:  # noqa: ANN001
    prod = db.get(Product, product_id)
    if prod is None or db.get(Facility, facility_id) is None:
        raise NotFoundError("Product or facility not found in the registry; policies never create registry records.")
    if prod.unit_of_measure and calc.norm_unit(prod.unit_of_measure) != calc.norm_unit(body.unit):
        raise DomainValidationError(
            f"Policy unit '{body.unit}' does not match the product's registered unit '{prod.unit_of_measure}'."
        )
    lt = calc.lead_time_to_days(body.lead_time.value, body.lead_time.unit) if body.lead_time else None
    pin = calc.PolicyIn(
        0,
        version,
        body.unit,
        body.safety_stock,
        body.reorder_point,
        body.minimum,
        body.maximum,
        body.target,
        lt,
        body.review_period_days,
        body.service_level,
    )
    problems = calc.policy_problems(pin)
    if problems:
        raise DomainValidationError(" ".join(problems))
    now = utcnow()
    p = InventoryPolicy(
        product_id=product_id, facility_id=facility_id, version=version, status=PolicyStatus.ACTIVE, unit=body.unit, safety_stock=body.safety_stock,
        reorder_point=body.reorder_point, minimum=body.minimum, maximum=body.maximum, target=body.target, review_period_days=body.review_period_days,
        lead_time_days=lt, lead_time_input=body.lead_time.model_dump() if body.lead_time else None, service_level=body.service_level,
        effective_from=body.effective_from or now, effective_to=body.effective_to, owner=body.owner, provenance=body.provenance, note=body.note,
        created_by=actor.name, created_at=now,
    )  # fmt: skip
    db.add(p)
    db.flush()
    return p


@router.post("/policies", status_code=201, responses=ERR, summary="Create the first policy for a product at a facility")
def create_policy(body: PolicyCreate, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        if db.scalar(
            select(InventoryPolicy.id)
            .where(InventoryPolicy.product_id == body.product_id, InventoryPolicy.facility_id == body.facility_id)
            .limit(1)
        ):
            raise ConflictError(
                "A policy already exists for this product and facility. Revise it to create a new version."
            )
        p = _new_policy(db, body.product_id, body.facility_id, body, 1, actor)
        audit.record(db, actor, "inventory.policy.create", target_type="INVENTORY_POLICY", target_id=p.id, details={"product_id": p.product_id, "facility_id": p.facility_id, "version": 1}, request_id=request_id_of(request))  # fmt: skip
        return _policy(p)


@router.post(
    "/policies/{policy_id}/revise",
    status_code=201,
    responses=ERR,
    summary="Supersede an active policy with a new version (history is kept)",
)
def revise_policy(
    policy_id: IdPath, body: PolicyBody, db: DbSession, request: Request, actor: Editor
) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        old = _get(db, InventoryPolicy, policy_id, "Policy")
        if old.status is not PolicyStatus.ACTIVE:
            raise ConflictError("Only the active version of a policy can be revised.")
        top = (
            db.scalar(
                select(func.max(InventoryPolicy.version)).where(
                    InventoryPolicy.product_id == old.product_id, InventoryPolicy.facility_id == old.facility_id
                )
            )
            or old.version
        )
        old.status, old.effective_to = PolicyStatus.SUPERSEDED, min(old.effective_to or utcnow(), utcnow())
        db.flush()
        p = _new_policy(db, old.product_id, old.facility_id, body, top + 1, actor)
        audit.record(db, actor, "inventory.policy.revise", target_type="INVENTORY_POLICY", target_id=p.id, details={"supersedes": old.id, "version": p.version}, request_id=request_id_of(request))  # fmt: skip
        return _policy(p)


@router.post(
    "/policies/{policy_id}/retire", responses=ERR, summary="Retire the active policy (no hard delete; history is kept)"
)
def retire_policy(policy_id: IdPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        p = _get(db, InventoryPolicy, policy_id, "Policy")
        if p.status is not PolicyStatus.ACTIVE:
            raise ConflictError("This policy version is already superseded.")
        p.status, p.effective_to = PolicyStatus.SUPERSEDED, utcnow()
        audit.record(
            db,
            actor,
            "inventory.policy.retire",
            target_type="INVENTORY_POLICY",
            target_id=p.id,
            details={},
            request_id=request_id_of(request),
        )
        return _policy(p)


@router.get("/policies", summary="Policies (active by default)")
def policies(
    db: DbSession,
    _a: Viewer,
    limit: Lim = 50,
    offset: Off = 0,
    product_id: Ident = None,
    facility_id: Ident = None,
    include_superseded: bool = False,
) -> dict[str, Any]:
    q = select(InventoryPolicy)
    if product_id:
        q = q.where(InventoryPolicy.product_id == product_id)
    if facility_id:
        q = q.where(InventoryPolicy.facility_id == facility_id)
    if not include_superseded:
        q = q.where(InventoryPolicy.status == PolicyStatus.ACTIVE)
    return _page(
        db,
        q.order_by(InventoryPolicy.product_id, InventoryPolicy.facility_id, InventoryPolicy.version.desc()),
        limit,
        offset,
        _policy,
    )


@router.get(
    "/policies/missing", summary="Product-facility pairs with inventory but no active policy (missing is not zero)"
)
def missing_policies(db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0) -> dict[str, Any]:
    have = set(
        db.execute(
            select(InventoryPolicy.product_id, InventoryPolicy.facility_id).where(
                InventoryPolicy.status == PolicyStatus.ACTIVE
            )
        ).all()
    )
    pairs = sorted(
        set(db.execute(select(InventoryPosition.product_id, InventoryPosition.facility_id).distinct()).all()) - have
    )
    page = pairs[offset : offset + limit]
    pn = dict(db.execute(select(Product.id, Product.name)).all()) if page else {}
    fn = dict(db.execute(select(Facility.id, Facility.name)).all()) if page else {}
    return {
        "items": [
            {"product_id": p, "facility_id": f, "product_name": pn.get(p), "facility_name": fn.get(f)} for p, f in page
        ],
        "total": len(pairs),
        "limit": limit,
        "offset": offset,
    }


@router.get("/policies/{policy_id}", responses=ERR, summary="One policy version")
def policy_detail(policy_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _policy(_get(db, InventoryPolicy, policy_id, "Policy"))


# ----------------------------------------------------------------------------------------------------------- analyses


class AnalysisCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    impact_analysis_id: int | None = Field(
        default=None, ge=1, le=2**31 - 1, description="Cascading impact analysis to apply; omit for a baseline."
    )
    config: cfg.InventoryConfig | None = None


@router.post(
    "/analyses",
    status_code=202,
    responses={**ERR, 202: {"description": "Queued"}},
    summary="Queue a baseline or scenario stockout analysis (idempotent with a key)",
)
def create_analysis(
    body: AnalysisCreate, tasks: BackgroundTasks, response: Response, db: DbSession, request: Request, actor: Editor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")] = None,
) -> dict[str, Any]:  # fmt: skip
    _writes(request)
    lim = _limits(request)
    with db.begin():
        a, created = runs.create_analysis(
            db,
            body.config or cfg.InventoryConfig(),
            actor.name,
            lim,
            impact_analysis_id=body.impact_analysis_id,
            idempotency_key=idempotency_key,
        )
        if created:
            audit.record(db, actor, "inventory.analysis.queued", target_type="STOCKOUT_ANALYSIS", target_id=a.id, details={"kind": a.kind.value, "impact_analysis_id": a.impact_analysis_id, "config_hash": a.config_hash}, request_id=request_id_of(request))  # fmt: skip
        out = _analysis(a)
    if created:
        tasks.add_task(runs.execute_analysis, request_session_factory(request), out["id"], lim)
    else:
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.get("/analyses", summary="Analyses (newest first)")
def list_analyses(
    db: DbSession,
    _a: Viewer,
    limit: Lim = 20,
    offset: Off = 0,
    kind: AnalysisKind | None = None,
    status: SimStatus | None = None,
) -> dict[str, Any]:
    q = select(StockoutAnalysis)
    if kind:
        q = q.where(StockoutAnalysis.kind == kind)
    if status:
        q = q.where(StockoutAnalysis.status == status)
    return _page(db, q.order_by(StockoutAnalysis.id.desc()), limit, offset, _analysis)


@router.get("/analyses/compare", responses=ERR, summary="Baseline vs scenario analysis, pair by pair")
def compare(db: DbSession, _a: Viewer, baseline_id: Annotated[int, Query(ge=1, le=2**31 - 1)], scenario_id: Annotated[int, Query(ge=1, le=2**31 - 1)],
            limit: Lim = 100, offset: Off = 0) -> dict[str, Any]:  # fmt: skip
    b, s = (
        _get(db, StockoutAnalysis, baseline_id, "Baseline analysis"),
        _get(db, StockoutAnalysis, scenario_id, "Scenario analysis"),
    )
    for x in (b, s):
        if x.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
            raise DomainValidationError(f"Analysis {x.id} is {x.status.value}; only finished analyses can be compared.")
    reasons = []
    if b.kind is not AnalysisKind.BASELINE or s.kind is not AnalysisKind.SCENARIO:
        reasons.append({"code": "KIND", "text": "Compare a BASELINE (first) with a SCENARIO (second) analysis."})
    cb, cs = dict(b.config), dict(s.config)
    if cb != cs:
        reasons.append(
            {
                "code": "CONFIG_DIFFERS",
                "text": "The two analyses used different settings, so differences may come from the settings, not the scenario.",
            }
        )
    if (
        b.input_hash
        and s.input_hash
        and b.inputs
        and s.inputs
        and (b.inputs.get("positions"), b.inputs.get("demand_observations"), b.inputs.get("receipts"))
        != (s.inputs.get("positions"), s.inputs.get("demand_observations"), s.inputs.get("receipts"))
    ):
        reasons.append(
            {"code": "INPUTS_DIFFER", "text": "The two analyses read different inventory, demand or receipt records."}
        )
    fb = {
        (f.product_id, f.facility_id): f
        for f in db.scalars(select(StockoutFinding).where(StockoutFinding.analysis_id == b.id))
    }
    fs = {
        (f.product_id, f.facility_id): f
        for f in db.scalars(select(StockoutFinding).where(StockoutFinding.analysis_id == s.id))
    }
    items = []
    for k in sorted(set(fb) | set(fs)):
        x, y = fb.get(k), fs.get(k)
        items.append({
            "product_id": k[0], "facility_id": k[1], "product_name": (x or y).product_name, "facility_name": (x or y).facility_name,
            "baseline_status": x.status if x else None, "scenario_status": y.status if y else None, "change": y.change if y else None,
            "baseline_stockout_day": (x.projection or {}).get("stockout_within_day") if x else None,
            "scenario_stockout_day": ((y.scenario or {}).get("stockout_within_day") if (y and (y.scenario or {}).get("outcome") == "QUANTIFIED") else ((y.projection or {}).get("stockout_within_day") if y else None)),
            "scenario_stockout_day_worst": (y.scenario or {}).get("stockout_within_day_worst") if y else None,
            "scenario_outcome": (y.scenario or {}).get("outcome") if y else None, "scenario_finding_id": y.id if y else None, "baseline_finding_id": x.id if x else None,
        })  # fmt: skip
    not_assessable = [i for i in items if (i["change"] or "").startswith("UNKNOWN")]
    changed = [i for i in items if i["change"] not in (None, "UNCHANGED", "UNAFFECTED") and i not in not_assessable]
    page = changed[offset : offset + limit]
    return {
        "baseline_analysis_id": b.id, "scenario_analysis_id": s.id, "comparable": not reasons, "caveats": reasons,
        "pairs": len(items), "changed": len(changed), "not_assessable": len(not_assessable), "items": page, "total": len(changed), "limit": limit, "offset": offset,
        "impact_analysis_id": s.impact_analysis_id, "note": "Scenario results are hypothetical and conditional. Unquantified exposure is shown as bounds, never as a precise loss.",
    }  # fmt: skip


@router.get(
    "/analyses/{analysis_id}", responses=ERR, summary="Status, settings, input hash, summary, coverage and safe error"
)
def get_analysis(analysis_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _analysis(_get(db, StockoutAnalysis, analysis_id, "Analysis"), full=True)


@router.post(
    "/analyses/{analysis_id}/cancel", responses=ERR, summary="Cancel a queued analysis, or ask a running one to stop"
)
def cancel(analysis_id: IdPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        a = _get(db, StockoutAnalysis, analysis_id, "Analysis")
        if a.requested_by != actor.name and actor.role < Role.REVIEWER:
            raise ForbiddenError("Only the requester or a reviewer can cancel this analysis.")
        runs.cancel_analysis(db, a)
        audit.record(db, actor, "inventory.analysis.cancel", target_type="STOCKOUT_ANALYSIS", target_id=a.id, details={"status": a.status.value}, request_id=request_id_of(request))  # fmt: skip
        return _analysis(a)


def _ready(db, analysis_id: int) -> StockoutAnalysis:  # noqa: ANN001
    a = _get(db, StockoutAnalysis, analysis_id, "Analysis")
    if a.status in (SimStatus.QUEUED, SimStatus.RUNNING):
        raise ConflictError(f"Analysis is {a.status.value}; results are available when it finishes.")
    if a.status in (SimStatus.FAILED, SimStatus.CANCELLED):
        raise ConflictError(f"Analysis {a.status.value.lower()}; it has no results.")
    return a


@router.get(
    "/analyses/{analysis_id}/summary",
    responses=ERR,
    summary="Counts by status, missing-data ranking and the disclaimer",
)
def summary(analysis_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    a = _ready(db, analysis_id)
    return {
        "analysis": _analysis(a),
        "summary": a.summary,
        "coverage": a.coverage,
        "warnings": a.warnings,
        "failures": a.failures,
        "inputs": a.inputs,
        "disclaimer": cfg.DISCLAIMER,
    }


@router.get(
    "/analyses/{analysis_id}/findings",
    responses=ERR,
    summary="Findings, most severe first (current shortage, projected stockout, low coverage, within policy, then unknown / insufficient data)",
)
def findings(
    analysis_id: IdPath, db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0, status: str | None = Query(default=None, max_length=32),
    product_id: Ident = None, facility_id: Ident = None, confidence: Literal["LOW", "MEDIUM", "HIGH"] | None = None,
    change: str | None = Query(default=None, max_length=48), q: Annotated[str | None, Query(max_length=100)] = None,
) -> dict[str, Any]:  # fmt: skip
    _ready(db, analysis_id)
    stmt = select(StockoutFinding).where(StockoutFinding.analysis_id == analysis_id)
    for col, v in (
        (StockoutFinding.status, status),
        (StockoutFinding.product_id, product_id),
        (StockoutFinding.facility_id, facility_id),
        (StockoutFinding.confidence, confidence),
        (StockoutFinding.change, change),
    ):
        if v:
            stmt = stmt.where(col == v)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(StockoutFinding.product_name).like(like)
            | func.lower(StockoutFinding.facility_name).like(like)
            | func.lower(StockoutFinding.product_code).like(like)
        )
    return _page(db, stmt.order_by(StockoutFinding.severity_rank.desc(), StockoutFinding.id), limit, offset, _finding)


@router.get(
    "/analyses/{analysis_id}/findings/{finding_id}",
    responses=ERR,
    summary="One finding with quantities, projection, scenario, assumptions and evidence",
)
def finding_detail(analysis_id: IdPath, finding_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    _ready(db, analysis_id)
    f = db.get(StockoutFinding, finding_id)
    if f is None or f.analysis_id != analysis_id:
        raise NotFoundError("Finding not found.")
    return _finding(f, full=True)
