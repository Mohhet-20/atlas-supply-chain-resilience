"""Asynchronous, tenant-scoped, expiring exports (CSV with formula-injection protection, or JSON).

The file name is generated (``<export_uid>.<ext>``) and the directory is fixed, so no user input ever reaches a path. Only the
requesting organisation can see or download a job, and the rows come from a session scoped to that organisation; files are
deleted when they expire.
"""

from __future__ import annotations

import csv
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.auth import Actor
from app.core.config import Settings
from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.ingestion.export import neutralize
from app.models.alerting import Alert
from app.models.facility import Facility
from app.models.integrations import ExportJob, IntegrationRun
from app.models.product import Product
from app.models.supplier import Supplier
from app.models.types import utcnow

COLUMNS: dict[str, tuple[Any, tuple[str, ...], dict[str, str]]] = {
    "suppliers": (Supplier, ("supplier_code", "name", "country_or_region", "category", "status", "provenance", "updated_at"), {"status": "status"}),
    "facilities": (Facility, ("facility_code", "name", "facility_type", "country_or_region", "status", "provenance", "updated_at"), {"status": "status"}),
    "products": (Product, ("product_code", "name", "category", "unit_of_measure", "status", "provenance", "updated_at"), {"status": "status"}),
    "alerts": (Alert, ("alert_uid", "title", "severity", "priority", "status", "source", "detector", "created_at"), {"status": "status", "severity": "severity"}),
    "integration_runs": (IntegrationRun, ("run_uid", "mode", "trigger", "status", "discovered", "processed", "created", "skipped", "rejected", "failed", "started_at", "finished_at"), {"status": "status"}),
}  # fmt: skip


def exports_dir(cfg: Settings) -> Path:
    d = Path(cfg.data_dir) / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_job(db: Session, actor: Actor, dataset: str, fmt: str, filters: dict[str, str]) -> ExportJob:
    if dataset not in COLUMNS:
        raise DomainValidationError(f"dataset must be one of: {', '.join(COLUMNS)}")
    if fmt not in ("csv", "json"):
        raise DomainValidationError("format must be csv or json")
    allowed = COLUMNS[dataset][2]
    bad = sorted(set(filters) - set(allowed))
    if bad:
        raise DomainValidationError(f"unsupported filters for {dataset}: {', '.join(bad)}")
    job = ExportJob(
        export_uid="EX-" + uuid.uuid4().hex[:12].upper(),
        organization_id=actor.org_id,
        dataset=dataset,
        format=fmt,
        filters=filters,
        status="queued",
        created_by=actor.name,
    )
    db.add(job)
    db.flush()
    return job


def get_job(db: Session, export_uid: str) -> ExportJob:
    job = db.scalar(select(ExportJob).where(ExportJob.export_uid == export_uid))
    if job is None:
        raise NotFoundError("Export not found.")
    return job


def _cell(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return getattr(v, "value", v)


def run_job(
    factory: Callable[[], Session], cfg: Settings, org_id: int, export_uid: str, now: datetime | None = None
) -> str:
    from app.db.tenancy import scope_session
    from app.integrations import events

    now = now or utcnow()
    with factory() as db:
        scope_session(db, org_id)
        job = get_job(db, export_uid)
        # Atomic claim: a duplicate trigger (retry, second worker) finds the job no longer queued and does nothing.
        claimed = db.execute(
            update(ExportJob)
            .where(ExportJob.export_uid == export_uid, ExportJob.status == "queued")
            .values(status="running")
            .execution_options(synchronize_session=False)
        ).rowcount
        db.commit()
        if claimed != 1:
            db.refresh(job)
            return job.status
        db.refresh(job)
        try:
            model, cols, filt = COLUMNS[job.dataset]
            q = select(model)
            for k, v in (job.filters or {}).items():
                col = getattr(model, filt[k])
                q = q.where(
                    col
                    == (v.upper() if job.dataset in ("suppliers", "facilities", "products") or k == "severity" else v)
                )
            q = q.order_by(model.id).limit(cfg.export_max_rows + 1)
            rows = []
            truncated = False
            for obj in db.scalars(q):
                if len(rows) >= cfg.export_max_rows:
                    truncated = True
                    break
                rows.append([_cell(getattr(obj, c)) for c in cols])
            name = f"{job.export_uid}.{job.format}"
            path = exports_dir(cfg) / name
            with path.open("w", encoding="utf-8", newline="") as fh:
                if job.format == "csv":
                    w = csv.writer(fh, lineterminator="\r\n")
                    w.writerow([neutralize(c) for c in cols])
                    for r in rows:
                        w.writerow([neutralize(c) for c in r])
                else:
                    json.dump(
                        {
                            "dataset": job.dataset,
                            "columns": list(cols),
                            "rows": [dict(zip(cols, r, strict=True)) for r in rows],
                            "truncated": truncated,
                        },
                        fh,
                        default=str,
                    )
            job.status, job.row_count, job.file_name, job.finished_at = "ready", len(rows), name, now
            job.expires_at = now + timedelta(minutes=cfg.export_ttl_minutes)
            job.error = f"truncated at {cfg.export_max_rows} rows" if truncated else None
            events.emit(
                db, org_id, "export.ready", {"export": job.export_uid, "dataset": job.dataset, "rows": len(rows)}
            )
        except Exception as e:  # noqa: BLE001
            db.rollback()
            job = get_job(db, export_uid)
            job.status, job.error, job.finished_at = "failed", type(e).__name__, now
        db.commit()
        return job.status


def file_for(cfg: Settings, job: ExportJob, now: datetime | None = None) -> Path:
    now = now or utcnow()
    exp = (
        job.expires_at if job.expires_at is None or job.expires_at.tzinfo else job.expires_at.replace(tzinfo=now.tzinfo)
    )
    if job.status == "expired" or (exp is not None and exp <= now):
        raise ConflictError("This export has expired. Create a new one.")
    if job.status != "ready" or not job.file_name:
        raise ConflictError(f"The export is {job.status}, not ready.")
    path = exports_dir(cfg) / job.file_name
    if not path.is_file() or path.parent != exports_dir(cfg):
        raise NotFoundError("The export file is no longer available.")
    return path


def sweep_expired(factory: Callable[[], Session], cfg: Settings, now: datetime | None = None) -> int:
    """Delete expired files (all organisations; a platform task, so it runs unscoped)."""
    now = now or utcnow()
    n = 0
    with factory() as db:
        for job in db.scalars(select(ExportJob).where(ExportJob.status == "ready", ExportJob.expires_at <= now)):
            if job.file_name:
                (exports_dir(cfg) / job.file_name).unlink(missing_ok=True)
            job.status, job.file_name = "expired", None
            n += 1
        db.commit()
    return n
