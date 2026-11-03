"""Deterministic master-data quality scoring with explicit dimensions and actionable issues.

A quality score is a diagnostic indicator, not a guarantee that a record is factually correct.
Dimensions: completeness, validity, consistency, uniqueness, freshness, traceability, reference_integrity.
Missing information (MISSING) is distinguished from invalid information (INVALID). Optional ("recommended") fields
never lower a score; they only produce informational issues and a separate coverage figure.
"""

from __future__ import annotations

import re
import time
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import Actor
from app.models.enums import CandidateStatus, CandidateTier, EntityStatus, LocationStatus, RegistryType
from app.models.facility import Facility
from app.models.ingestion import RecordLineage
from app.models.product import Product, ProductLink
from app.models.registry import AuditEvent, DuplicateCandidate, QualityAssessment, QualityRun
from app.models.relationship import SupplyRelationship
from app.models.source import DataSource
from app.models.supplier import Supplier
from app.models.types import utcnow
from app.registry import audit
from app.registry.config import QualityConfig, config_hash
from app.registry.entities import MODELS, Entity, active_entities, code_of
from app.registry.views import KIND

DIMENSIONS = (
    "completeness",
    "validity",
    "consistency",
    "uniqueness",
    "freshness",
    "traceability",
    "reference_integrity",
)
EXPLANATIONS = {
    "completeness": "Weighted share of required fields that are populated (recommended fields are excluded).",
    "validity": "1 minus the share of applicable value checks that fail (format, range, whitespace, bounds).",
    "consistency": "1 minus the share of cross-attribute / cross-record checks that fail.",
    "uniqueness": "1 minus the largest penalty from open or confirmed duplicate candidates (rejected pairs do not count).",
    "freshness": "1 within fresh_days of the last update, falling linearly to 0 at stale_days.",
    "traceability": "0.5 if a source identifier is present + 0.5 if ingestion lineage or a creation audit event exists.",
    "reference_integrity": "1 minus the share of references (owner/parent/manufacturer/relationship endpoints) that are broken.",
}
_UOM = re.compile(r"^[A-Za-z][A-Za-z0-9 /._%-]{0,31}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class Ctx:
    """Bulk-loaded lookups so assessing N entities costs a constant number of queries."""

    def __init__(self, session: Session, rtype: RegistryType) -> None:
        self.rtype = rtype
        self.lineage = {
            eid: n
            for eid, n in session.execute(
                select(RecordLineage.entity_id, func.count())
                .where(RecordLineage.entity_type == KIND[rtype])
                .group_by(RecordLineage.entity_id)
            )
        }
        self.created_by_audit = {
            int(t)
            for (t,) in session.execute(
                select(AuditEvent.target_id).where(
                    AuditEvent.action == "registry.create", AuditEvent.target_type == rtype.value
                )
            )
            if t and str(t).isdigit()
        }
        self.candidates: dict[int, list[DuplicateCandidate]] = defaultdict(list)
        open_status = [
            CandidateStatus.NEW,
            CandidateStatus.UNDER_REVIEW,
            CandidateStatus.REOPENED,
            CandidateStatus.DEFERRED,
            CandidateStatus.CONFIRMED_DUPLICATE,
        ]
        for c in session.scalars(
            select(DuplicateCandidate).where(
                DuplicateCandidate.entity_type == rtype,
                DuplicateCandidate.status.in_(open_status),
                DuplicateCandidate.tier != CandidateTier.INSUFFICIENT,
            )
        ):
            self.candidates[c.left_id].append(c)
            self.candidates[c.right_id].append(c)
        self.supplier_status = {i: s for i, s in session.execute(select(Supplier.id, Supplier.status))}
        self.supplier_merged = {
            i for (i,) in session.execute(select(Supplier.id).where(Supplier.merged_into_id.is_not(None)))
        }
        self.facility_merged = {
            i for (i,) in session.execute(select(Facility.id).where(Facility.merged_into_id.is_not(None)))
        }
        self.product_merged = {
            i for (i,) in session.execute(select(Product.id).where(Product.merged_into_id.is_not(None)))
        }
        self.supplier_rel_count = dict(
            session.execute(
                select(SupplyRelationship.supplier_id, func.count())
                .where(SupplyRelationship.supplier_id.is_not(None))
                .group_by(SupplyRelationship.supplier_id)
            ).all()
        )
        self.product_link_count = dict(
            session.execute(select(ProductLink.product_id, func.count()).group_by(ProductLink.product_id)).all()
        )
        # relationship endpoints that point at merged (tombstoned) facilities / suppliers
        self.broken_by_facility: dict[int, int] = defaultdict(int)
        for rel in session.scalars(select(SupplyRelationship)):
            for fid in (rel.source_facility_id, rel.target_facility_id):
                if fid in self.facility_merged:
                    self.broken_by_facility[fid] += 1
        self.facility_status = {i: s for i, s in session.execute(select(Facility.id, Facility.status))}


def _issue(
    dim: str, kind: str, severity: str, field: str | None, message: str, remediation: str, **extra: Any
) -> dict[str, Any]:
    return {
        "dimension": dim,
        "kind": kind,
        "severity": severity,
        "field": field,
        "message": message,
        "remediation": remediation,
        **extra,
    }


def assess_entity(rtype: RegistryType, e: Entity, cfg: QualityConfig, ctx: Ctx, now: datetime) -> dict[str, Any]:
    rules = cfg.entities[rtype.value]
    issues: list[dict[str, Any]] = []
    dims: dict[str, dict[str, Any]] = {}

    # completeness
    total = sum(f.weight for f in rules.fields if f.required) or 1.0
    got = 0.0
    for f in rules.fields:
        if not f.required:
            continue
        v = getattr(e, f.name, None)
        if v is None or (isinstance(v, str) and not v.strip()):
            issues.append(
                _issue(
                    "completeness",
                    "MISSING",
                    "ERROR",
                    f.name,
                    f"Required field '{f.name}' is empty.",
                    f"Provide a value for {f.name}.",
                )
            )
        else:
            got += f.weight
    rec_present = sum(1 for n in rules.recommended if getattr(e, n, None) not in (None, ""))
    for n in rules.recommended:
        if getattr(e, n, None) in (None, ""):
            issues.append(
                _issue(
                    "completeness",
                    "MISSING_RECOMMENDED",
                    "INFO",
                    n,
                    f"Recommended field '{n}' is empty (not scored).",
                    f"Add {n} if it is known; do not guess.",
                )
            )
    dims["completeness"] = {
        "score": round(got / total, 4),
        "detail": f"{got:g}/{total:g} required weight populated",
        "recommended_coverage": round(rec_present / len(rules.recommended), 4) if rules.recommended else None,
    }

    # validity
    checks = fails = 0

    def check(ok: bool, field: str, message: str, fix: str) -> None:
        nonlocal checks, fails
        checks += 1
        if not ok:
            fails += 1
            issues.append(_issue("validity", "INVALID", "ERROR", field, message, fix))

    for fname in ("name", "legal_name", "address", "city", "country_or_region", "category"):
        v = getattr(e, fname, None)
        if isinstance(v, str) and v:
            check(
                v == v.strip() and not _CONTROL.search(v),
                fname,
                f"'{fname}' has leading/trailing whitespace or control characters.",
                f"Clean the value of {fname}.",
            )
    if getattr(e, "country_or_region", None):
        check(
            not e.country_or_region.strip().isdigit(),
            "country_or_region",
            "Country/region is purely numeric.",
            "Use a country or region name.",
        )
    if rtype is RegistryType.FACILITY:
        if e.latitude is not None and e.longitude is not None:
            check(
                -90 <= e.latitude <= 90 and -180 <= e.longitude <= 180,
                "latitude",
                "Coordinates are out of range.",
                "Correct or clear the coordinates.",
            )
            check(
                not (e.latitude == 0 and e.longitude == 0),
                "latitude",
                "Coordinates (0,0) are a common placeholder, not a real site.",
                "Clear the coordinates if they are unknown.",
            )
        if e.capacity_value is not None:
            check(
                e.capacity_value >= 0 and bool(e.capacity_unit),
                "capacity_value",
                "Capacity must be non-negative and carry a unit.",
                "Fix the capacity value/unit.",
            )
    if rtype is RegistryType.PRODUCT and e.unit_of_measure:
        check(
            bool(_UOM.match(e.unit_of_measure)),
            "unit_of_measure",
            "Unit of measure is not a plain unit label.",
            "Use a short unit label such as EA or KG.",
        )
    if e.external_id:
        check(
            bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._\-/#]{0,63}\Z", e.external_id)),
            "external_id",
            "External identifier violates the identifier policy.",
            "Use letters/digits then . _ - / #.",
        )
    dims["validity"] = {
        "score": round(1 - fails / checks, 4) if checks else 1.0,
        "detail": f"{checks - fails}/{checks} checks passed",
    }

    # consistency
    c_checks = c_fails = 0

    def consistent(ok: bool, kind: str, field: str | None, message: str, fix: str, severity: str = "WARNING") -> None:
        nonlocal c_checks, c_fails
        c_checks += 1
        if not ok:
            c_fails += 1
            issues.append(_issue("consistency", kind, severity, field, message, fix))

    if rtype is RegistryType.FACILITY:
        has_coords = e.latitude is not None
        consistent(
            (e.location_status is LocationStatus.UNKNOWN) == (not has_coords), "CONFLICT", "location_status",
            "location_status disagrees with whether coordinates exist.", "Set location_status to UNKNOWN when there are no coordinates.",
        )  # fmt: skip
        if e.owner_supplier_id:
            owner_status = ctx.supplier_status.get(e.owner_supplier_id)
            consistent(
                not (e.status is EntityStatus.ACTIVE and owner_status is EntityStatus.INACTIVE),
                "CONFLICT",
                "owner_supplier_id",
                "Active facility is owned by an inactive supplier.",
                "Review the facility or owner status.",
            )
    if rtype is RegistryType.SUPPLIER:
        consistent(
            not (e.status is EntityStatus.INACTIVE and ctx.supplier_rel_count.get(e.id, 0) > 0),
            "CONFLICT",
            "status",
            "Inactive supplier still has supply relationships.",
            "Review the supplier status or end its relationships.",
        )
    if rtype is RegistryType.PRODUCT:
        consistent(
            not (e.retired_at is not None and ctx.product_link_count.get(e.id, 0) > 0),
            "CONFLICT",
            "retired_at",
            "Retired product still has product links.",
            "Review the links or reactivate the product.",
        )
        consistent(
            (e.retired_at is not None) <= (e.status is not EntityStatus.ACTIVE),
            "CONFLICT",
            "status",
            "Retired product is marked ACTIVE.",
            "Set status to INACTIVE for retired products.",
        )
    dims["consistency"] = {
        "score": round(1 - c_fails / c_checks, 4) if c_checks else None,
        "detail": f"{c_checks - c_fails}/{c_checks} checks passed" if c_checks else "no applicable checks",
    }

    # uniqueness
    penalty, worst = 0.0, None
    for cand in ctx.candidates.get(e.id, []):
        p = (
            cfg.uniqueness_confirmed_penalty if cand.status is CandidateStatus.CONFIRMED_DUPLICATE
            else cfg.uniqueness_open_strong_penalty if cand.tier is CandidateTier.STRONG
            else cfg.uniqueness_open_possible_penalty
        )  # fmt: skip
        other = cand.right_id if cand.left_id == e.id else cand.left_id
        issues.append(
            _issue(
                "uniqueness",
                "DUPLICATE",
                "WARNING" if p < 0.5 else "ERROR",
                None,
                f"{cand.tier.value.title()} duplicate candidate with record {other} ({cand.status.value}).",
                "Review the candidate and confirm, reject or merge it.",
                candidate_id=cand.id,
                other_id=other,
            )
        )
        if p > penalty:
            penalty, worst = p, cand.id
    dims["uniqueness"] = {
        "score": round(1 - penalty, 4),
        "detail": f"largest penalty {penalty:g}" + (f" from candidate {worst}" if worst else ""),
    }

    # freshness
    updated = getattr(e, "updated_at", None) or e.created_at
    age = max(0.0, (now - updated).total_seconds() / 86400)
    if age <= cfg.fresh_days:
        fresh = 1.0
    elif age >= cfg.stale_days:
        fresh = 0.0
    else:
        fresh = 1 - (age - cfg.fresh_days) / (cfg.stale_days - cfg.fresh_days)
    if age > cfg.fresh_days:
        issues.append(
            _issue(
                "freshness",
                "STALE",
                "WARNING" if age < cfg.stale_days else "ERROR",
                None,
                f"Last updated {int(age)} days ago (expected within {cfg.fresh_days}).",
                "Confirm the record with its source and update it.",
            )
        )
    dims["freshness"] = {"score": round(fresh, 4), "detail": f"{int(age)} days since last update"}

    # traceability
    has_source = e.source_id is not None and e.external_id is not None
    has_lineage = ctx.lineage.get(e.id, 0) > 0 or e.id in ctx.created_by_audit
    if not has_source:
        issues.append(
            _issue(
                "traceability",
                "UNTRACEABLE",
                "INFO",
                "source",
                "No source identifier is recorded.",
                "Attach the source system's identifier (registry identifiers API).",
            )
        )
    if not has_lineage:
        issues.append(
            _issue(
                "traceability",
                "UNTRACEABLE",
                "INFO",
                "lineage",
                "No ingestion lineage or creation audit event exists.",
                "Re-ingest from the source so lineage is recorded.",
            )
        )
    dims["traceability"] = {
        "score": 0.5 * has_source + 0.5 * has_lineage,
        "detail": f"source id: {'yes' if has_source else 'no'}; lineage/audit: {'yes' if has_lineage else 'no'}",
    }

    # reference integrity
    r_checks = r_fails = 0

    def ref(ok: bool, field: str, message: str, fix: str) -> None:
        nonlocal r_checks, r_fails
        r_checks += 1
        if not ok:
            r_fails += 1
            issues.append(_issue("reference_integrity", "BROKEN_REFERENCE", "ERROR", field, message, fix))

    if rtype is RegistryType.SUPPLIER and e.parent_supplier_id:
        ref(
            e.parent_supplier_id not in ctx.supplier_merged,
            "parent_supplier_id",
            "Parent supplier was merged away.",
            "Point to the surviving record.",
        )
    if rtype is RegistryType.FACILITY:
        if e.owner_supplier_id:
            ref(
                e.owner_supplier_id not in ctx.supplier_merged,
                "owner_supplier_id",
                "Owner supplier was merged away.",
                "Point to the surviving record.",
            )
        ref(
            ctx.broken_by_facility.get(e.id, 0) == 0,
            "relationships",
            "Relationships reference a merged facility.",
            "Reassign relationships to the surviving facility.",
        )
    if rtype is RegistryType.PRODUCT and e.manufacturer_supplier_id:
        ref(
            e.manufacturer_supplier_id not in ctx.supplier_merged,
            "manufacturer_supplier_id",
            "Manufacturer was merged away.",
            "Point to the surviving record.",
        )
    dims["reference_integrity"] = {
        "score": round(1 - r_fails / r_checks, 4) if r_checks else None,
        "detail": f"{r_checks - r_fails}/{r_checks} checks passed" if r_checks else "no references to check",
    }

    weights = rules.dimension_weights
    applicable = {d: weights[d] for d in DIMENSIONS if dims[d]["score"] is not None}
    wsum = sum(applicable.values())
    overall = round(sum(dims[d]["score"] * w for d, w in applicable.items()) / wsum, 4) if wsum else None
    for d in DIMENSIONS:
        dims[d]["explanation"] = EXPLANATIONS[d]
        dims[d]["weight"] = weights[d]
        dims[d]["applicable"] = dims[d]["score"] is not None
    return {"overall": overall, "dimensions": dims, "issues": issues}


def run_assessment(
    session: Session,
    cfg: QualityConfig,
    entity_types: list[RegistryType] | None,
    actor: Actor,
    request_id: str | None = None,
) -> QualityRun:
    started = time.monotonic()
    types = entity_types or list(RegistryType)
    chash = config_hash(cfg)
    previous = session.scalar(select(QualityRun).order_by(QualityRun.id.desc()).limit(1))
    run = QualityRun(
        run_uid=f"QLT-{uuid.uuid4().hex[:16].upper()}",
        config_version=cfg.version,
        config_hash=chash,
        actor=actor.name,
        entity_types=[t.value for t in types],
        dimension_averages={},
        started_at=utcnow(),
    )
    session.add(run)
    session.flush()
    if previous is not None and previous.config_hash != chash:
        audit.record(
            session, actor, "quality.policy_changed", target_type="QUALITY_CONFIG", target_id=cfg.version,
            details={"previous_version": previous.config_version, "new_version": cfg.version}, request_id=request_id,
        )  # fmt: skip
    now = utcnow()
    count = 0
    totals: list[float] = []
    dim_sums: dict[str, list[float]] = defaultdict(list)
    for rtype in types:
        ctx = Ctx(session, rtype)
        for e in active_entities(session, rtype):
            res = assess_entity(rtype, e, cfg, ctx, now)
            session.add(
                QualityAssessment(
                    run_id=run.id, entity_type=rtype, entity_id=e.id, entity_version=e.version, source_id=e.source_id,
                    config_version=cfg.version, overall=res["overall"], dimensions=res["dimensions"], issues=res["issues"],
                    issue_count=len(res["issues"]), assessed_at=now,
                )
            )  # fmt: skip
            count += 1
            if res["overall"] is not None:
                totals.append(res["overall"])
            for d, v in res["dimensions"].items():
                if v["score"] is not None:
                    dim_sums[d].append(v["score"])
    session.flush()
    run.entities_assessed = count
    run.average_overall = round(sum(totals) / len(totals), 4) if totals else None
    run.dimension_averages = {d: round(sum(v) / len(v), 4) for d, v in dim_sums.items()}
    run.finished_at = utcnow()
    run.duration_ms = int((time.monotonic() - started) * 1000)
    session.flush()
    audit.record(
        session, actor, "quality.run", target_type="QUALITY_RUN", target_id=run.run_uid,
        details={"config_version": cfg.version, "entities": count, "average_overall": run.average_overall}, request_id=request_id,
    )  # fmt: skip
    return run


def latest_run(session: Session) -> QualityRun | None:
    return session.scalar(select(QualityRun).order_by(QualityRun.id.desc()).limit(1))


def run_out(run: QualityRun) -> dict[str, Any]:
    return {
        "id": run.id, "run_uid": run.run_uid, "config_version": run.config_version, "config_hash": run.config_hash,
        "actor": run.actor, "entity_types": run.entity_types, "started_at": run.started_at, "duration_ms": run.duration_ms,
        "entities_assessed": run.entities_assessed, "average_overall": run.average_overall, "dimension_averages": run.dimension_averages,
    }  # fmt: skip


def summary(session: Session, cfg: QualityConfig) -> dict[str, Any]:
    run = latest_run(session)
    if run is None:
        return {"has_data": False, "message": "No quality assessment has been run yet.", "trend": []}
    rows = session.scalars(select(QualityAssessment).where(QualityAssessment.run_id == run.id)).all()
    by_type: dict[str, list[float]] = defaultdict(list)
    kinds: dict[str, int] = defaultdict(int)
    low = 0
    by_source: dict[int | None, list[float]] = defaultdict(list)
    for a in rows:
        if a.overall is not None:
            by_type[a.entity_type.value].append(a.overall)
            by_source[a.source_id].append(a.overall)
            if a.overall < cfg.low_score_threshold:
                low += 1
        for issue in a.issues:
            kinds[issue["kind"]] += 1
    names = {i: n for i, n in session.execute(select(DataSource.id, DataSource.namespace))}
    trend = [
        {
            "run_id": r.id,
            "started_at": r.started_at,
            "average_overall": r.average_overall,
            "entities": r.entities_assessed,
        }
        for r in session.scalars(select(QualityRun).order_by(QualityRun.id.desc()).limit(20))
    ][::-1]
    return {
        "has_data": True,
        "run": run_out(run),
        "by_entity_type": {t: {"count": len(v), "average": round(sum(v) / len(v), 4)} for t, v in by_type.items()},
        "by_source": [
            {
                "source_id": sid,
                "namespace": names.get(sid) if sid else None,
                "count": len(v),
                "average": round(sum(v) / len(v), 4),
            }
            for sid, v in sorted(by_source.items(), key=lambda kv: (kv[0] is None, kv[0]))
        ],
        "issue_counts": dict(sorted(kinds.items())),
        "low_score_count": low,
        "low_score_threshold": cfg.low_score_threshold,
        "trend": trend,
        "disclaimer": "Quality scores are diagnostic indicators, not proof that a record is factually correct.",
    }


def summary_for_batch(session: Session, batch_id: int) -> dict[str, Any]:
    run = latest_run(session)
    if run is None:
        return {"has_data": False, "message": "No quality assessment has been run yet."}
    pairs = session.execute(
        select(RecordLineage.entity_type, RecordLineage.entity_id).where(RecordLineage.batch_id == batch_id).distinct()
    ).all()
    scores: list[float] = []
    for kind, eid in pairs:
        try:
            rtype = RegistryType(kind.value)
        except ValueError:
            continue
        a = session.scalar(
            select(QualityAssessment).where(
                QualityAssessment.run_id == run.id,
                QualityAssessment.entity_type == rtype,
                QualityAssessment.entity_id == eid,
            )
        )
        if a and a.overall is not None:
            scores.append(a.overall)
    return {
        "has_data": True,
        "batch_id": batch_id,
        "assessed": len(scores),
        "average": round(sum(scores) / len(scores), 4) if scores else None,
        "run_id": run.id,
    }


def list_issues(
    session: Session,
    *,
    entity_type: RegistryType | None,
    dimension: str | None,
    kind: str | None,
    severity: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    run = latest_run(session)
    if run is None:
        return {"items": [], "total": 0, "limit": limit, "offset": offset, "has_data": False}
    stmt = select(QualityAssessment).where(QualityAssessment.run_id == run.id, QualityAssessment.issue_count > 0)
    if entity_type:
        stmt = stmt.where(QualityAssessment.entity_type == entity_type)
    flat: list[dict[str, Any]] = []
    for a in session.scalars(stmt.order_by(QualityAssessment.overall.asc(), QualityAssessment.id).limit(3000)):
        model = MODELS[a.entity_type]
        for issue in a.issues:
            if dimension and issue["dimension"] != dimension:
                continue
            if kind and issue["kind"] != kind:
                continue
            if severity and issue["severity"] != severity:
                continue
            flat.append(
                {**issue, "entity_type": a.entity_type.value, "entity_id": a.entity_id, "entity_score": a.overall}
            )
    total = len(flat)
    page = flat[offset : offset + limit]
    names: dict[tuple[str, int], str] = {}
    for rtype in {RegistryType(i["entity_type"]) for i in page}:
        ids = {i["entity_id"] for i in page if i["entity_type"] == rtype.value}
        model = MODELS[rtype]
        for eid, name in session.execute(select(model.id, model.name).where(model.id.in_(ids))):
            names[(rtype.value, eid)] = name
    for i in page:
        i["entity_name"] = names.get((i["entity_type"], i["entity_id"]))
    return {"items": page, "total": total, "limit": limit, "offset": offset, "has_data": True, "run_id": run.id}


__all__ = ["assess_entity", "run_assessment", "summary", "list_issues", "summary_for_batch", "code_of"]
