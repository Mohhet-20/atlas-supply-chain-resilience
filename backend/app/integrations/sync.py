"""Synchronisation engine.

Safety properties (each covered by a test):
* One active run per integration, enforced by a partial unique index (not just application logic). Runs whose heartbeat is stale
  are reaped as failed so a crashed worker cannot block an integration forever.
* Each page is its own transaction; each record runs in a SAVEPOINT. A bad record is rejected/failed and recorded, and never
  rolls back its neighbours. A failed run leaves all previously committed records untouched.
* The checkpoint (``run.checkpoint.cursor``) is committed with the page it describes, so a failed run can be resumed from the last
  committed page. The incremental watermark (``SyncCheckpoint``) advances only after a fully successful run.
* Transient connector errors are retried with back-off per page; auth/permanent errors stop the run without retry.
* Duplicates are detected by idempotency key (``integration:external_id:timestamp``): re-delivered records are counted as skipped.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.alerting import service as S
from app.core.config import Settings
from app.core.errors import AtlasError, ConflictError, DomainValidationError
from app.integrations import events, secrets
from app.integrations.connectors import Connector, ConnectorError, get_connector
from app.integrations.mapping import MappingError, map_record
from app.integrations.webhooks import hosts_of
from app.models.integrations import Integration, IntegrationRun, SyncCheckpoint, SyncError
from app.models.types import utcnow

PAGE_SIZE = 100
STALE_AFTER = timedelta(minutes=15)
MAX_RECORDED_ERRORS = 100


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=utcnow().tzinfo)


def reap_stale(db: Session, integration_id: int, now: datetime) -> int:
    n = 0
    for r in db.scalars(
        select(IntegrationRun).where(
            IntegrationRun.integration_id == integration_id, IntegrationRun.status.in_(("queued", "running"))
        )
    ):
        beat = _aware(r.heartbeat_at or r.created_at)
        if beat and now - beat > STALE_AFTER:
            r.status, r.finished_at, r.error_summary = "failed", now, "abandoned: the worker stopped reporting"
            n += 1
    if n:
        db.flush()
    return n


def start_run(
    db: Session,
    integ: Integration,
    mode: str,
    trigger: str,
    actor: str,
    resume: bool = False,
    now: datetime | None = None,
) -> IntegrationRun:
    now = now or utcnow()
    if mode not in ("full", "incremental"):
        raise DomainValidationError("mode must be full or incremental")
    if not integ.enabled or integ.status in ("draft", "disabled"):
        raise ConflictError("The integration is not enabled. Configure, test and enable it first.")
    if trigger == "scheduled" and integ.circuit_open_until and _aware(integ.circuit_open_until) > now:
        raise ConflictError(
            "The circuit breaker is open after repeated failures; scheduled syncs resume after the cool-down."
        )
    reap_stale(db, integ.id, now)
    checkpoint: dict[str, Any] = {}
    if resume:
        last = db.scalar(
            select(IntegrationRun)
            .where(
                IntegrationRun.integration_id == integ.id, IntegrationRun.status.in_(("failed", "cancelled", "partial"))
            )
            .order_by(IntegrationRun.id.desc())
        )
        if last is not None and (last.checkpoint or {}).get("cursor"):
            checkpoint = {"cursor": last.checkpoint["cursor"], "resumed_from": last.run_uid}
    run = IntegrationRun(run_uid="IR-" + uuid.uuid4().hex[:12].upper(), organization_id=integ.organization_id, integration_id=integ.id, mode=mode, trigger=trigger, status="queued", checkpoint=checkpoint, heartbeat_at=now, created_by=actor)  # fmt: skip
    try:
        with db.begin_nested():
            db.add(run)
            db.flush()
    except IntegrityError:
        raise ConflictError("A synchronisation is already queued or running for this integration.") from None
    return run


class Writer:
    """Target: alerting alert events (tenant-scoped, idempotent, validated). The only target in this build."""

    def __init__(self, ctx: S.Ctx, actor: str, integration_uid: str) -> None:
        self.ctx, self.actor, self.uid = ctx, actor, integration_uid

    def write(self, db: Session, mapped: dict[str, Any]) -> str:
        body = S.EventBody(
            event_type=mapped["event_type"], source="int-" + self.uid.lower(), source_record_id=mapped["source_record_id"], idempotency_key=mapped["idempotency_key"],
            occurred_at=mapped["occurred_at"], provenance="IMPORTED_EXTERNAL", payload=mapped["payload"], entities=mapped["entities"],
        )  # fmt: skip
        _ev, dup, _summary = S.ingest_event(db, self.actor, body, self.ctx)
        return "skipped" if dup else "created"


def _record_error(db: Session, run: IntegrationRun, ref: str | None, code: str, message: str) -> None:
    if db.query(SyncError).filter(SyncError.run_id == run.id).count() < MAX_RECORDED_ERRORS:
        db.add(
            SyncError(
                organization_id=run.organization_id,
                run_id=run.id,
                record_ref=(ref or "")[:128] or None,
                code=code,
                message=message[:300],
            )
        )


def execute_run(
    factory: Callable[[], Session], cfg: Settings, org_id: int, run_uid: str, ctx: S.Ctx, connector: Connector | None = None,
    sleep: Callable[[float], None] = time.sleep, writer: Writer | None = None,
) -> str:  # fmt: skip
    """Run one queued sync to completion in its own tenant-scoped sessions. Returns the final status; never raises for data errors."""
    from app.db.tenancy import scope_session

    t0 = time.monotonic()
    db = factory()
    scope_session(db, org_id)
    try:
        run = db.scalar(select(IntegrationRun).where(IntegrationRun.run_uid == run_uid))
        integ = db.get(Integration, run.integration_id)
        conn = connector or get_connector(integ.provider)
        run.status, run.started_at, run.heartbeat_at = "running", utcnow(), utcnow()
        integ.status = "syncing"
        db.commit()
        credential = secrets.resolve_ref(integ.credential_ref)
        hosts = hosts_of(cfg)
        writer = writer or Writer(ctx, f"integration:{integ.integration_uid}", integ.integration_uid)
        cp = db.scalar(select(SyncCheckpoint).where(SyncCheckpoint.integration_id == integ.id))
        since = (cp.cursor or {}).get("since") if (cp and run.mode == "incremental") else None
        cursor = (run.checkpoint or {}).get("cursor")
        max_updated: str | None = (run.checkpoint or {}).get("max_updated")
        spec = integ.mapping or {}
        status, summary = "succeeded", None
        total = 0
        while True:
            db.refresh(run)
            if run.cancel_requested:
                status, summary = "cancelled", "cancelled on request between pages"
                break
            if time.monotonic() - t0 > cfg.sync_max_seconds:
                status, summary = (
                    "partial",
                    f"stopped at the {cfg.sync_max_seconds}s time limit; resume continues from the checkpoint",
                )
                break
            page = None
            for attempt in range(1, cfg.sync_page_retry_attempts + 1):
                try:
                    page = conn.fetch(integ.config or {}, credential, cursor, since, PAGE_SIZE, hosts)
                    break
                except ConnectorError as e:
                    if e.kind == "transient" and attempt < cfg.sync_page_retry_attempts:
                        run.retries += 1
                        db.commit()
                        sleep(min(2 ** (attempt - 1), 30))
                        continue
                    status, summary = "failed", f"{e.kind}: {e.message}"[:300]
                    break
            if page is None:
                break
            run.discovered += len(page.records)
            for rec in page.records:
                total += 1
                if total > cfg.sync_max_records:
                    status, summary = "partial", f"stopped at the {cfg.sync_max_records} record limit"
                    break
                ref = str(rec.get("external_id") or rec.get("id") or "")[:128]
                try:
                    with db.begin_nested():
                        mapped = map_record(spec, rec, integ.integration_uid)
                        if mapped["unmapped"] and spec.get("unknown") == "reject":
                            raise MappingError(f"unmapped fields: {', '.join(mapped['unmapped'])}")
                        outcome = writer.write(db, mapped)
                    run.processed += 1
                    setattr(run, outcome, getattr(run, outcome) + 1)
                    stamp = mapped["occurred_at"].isoformat()
                    max_updated = stamp if max_updated is None or stamp > max_updated else max_updated
                except MappingError as e:
                    run.rejected += 1
                    _record_error(db, run, ref, "mapping", str(e))
                except (AtlasError, ValueError) as e:  # validation or idempotency conflict from the target
                    run.rejected += 1
                    _record_error(db, run, ref, "rejected", getattr(e, "message", str(e)))
                except Exception as e:  # noqa: BLE001 - one bad record must not abort the page
                    run.failed += 1
                    _record_error(db, run, ref, "failed", type(e).__name__)
            if status == "partial" and summary and "limit" in summary:
                break
            cursor = page.next_cursor
            run.checkpoint = {**(run.checkpoint or {}), "cursor": cursor, "max_updated": max_updated}
            run.heartbeat_at = utcnow()
            db.commit()  # the page and its checkpoint become durable together
            if cursor is None:
                break
        if status == "succeeded" and (run.rejected or run.failed):
            status, summary = "partial", f"{run.rejected} rejected, {run.failed} failed record(s); see the error list"
        run.status, run.finished_at = status, utcnow()
        run.error_summary = summary
        run.duration_ms = int((time.monotonic() - t0) * 1000)
        _finish_integration(db, integ, run, cp, max_updated, cfg)
        events.emit(db, org_id, "integration.sync.failed" if status == "failed" else "integration.sync.completed", {"integration": integ.integration_uid, "run": run.run_uid, "status": status, "created": run.created, "skipped": run.skipped, "rejected": run.rejected, "failed": run.failed})  # fmt: skip
        db.commit()
        return status
    except Exception:
        db.rollback()
        run = db.scalar(select(IntegrationRun).where(IntegrationRun.run_uid == run_uid))
        if run is not None:
            run.status, run.finished_at, run.error_summary = "failed", utcnow(), "internal error; see the server log"
            integ = db.get(Integration, run.integration_id)
            integ.status, integ.last_failure_at, integ.error_summary = "failed", utcnow(), "internal error"
            db.commit()
        raise
    finally:
        db.close()


def _finish_integration(
    db: Session,
    integ: Integration,
    run: IntegrationRun,
    cp: SyncCheckpoint | None,
    max_updated: str | None,
    cfg: Settings | None = None,
) -> None:
    now = utcnow()
    # Circuit breaker: a failed run counts; any success closes it. After N consecutive failures scheduled runs pause for the
    # cool-down; a manual run is still allowed and acts as the half-open probe (success resets the counter).
    if run.status == "failed":
        integ.consecutive_failed_runs = (integ.consecutive_failed_runs or 0) + 1
        if cfg is not None and integ.consecutive_failed_runs >= cfg.circuit_failure_threshold:
            integ.circuit_open_until = now + timedelta(seconds=cfg.circuit_cooldown_seconds)
    elif run.status in ("succeeded", "partial"):
        integ.consecutive_failed_runs, integ.circuit_open_until = 0, None
    if run.status == "succeeded":
        integ.status, integ.last_success_at, integ.error_summary = "healthy", now, None
        if max_updated:
            if cp is None:
                db.add(
                    SyncCheckpoint(
                        organization_id=integ.organization_id, integration_id=integ.id, cursor={"since": max_updated}
                    )
                )
            else:
                cp.cursor = {"since": max_updated}  # watermark moves only after a fully successful run
    elif run.status == "partial":
        integ.status, integ.last_success_at, integ.error_summary = "degraded", now, run.error_summary
    elif run.status == "cancelled":
        integ.status = "connected"
    else:
        integ.status, integ.last_failure_at, integ.error_summary = "failed", now, run.error_summary


def run_due(
    factory: Callable[[], Session], cfg: Settings, org_id: int, ctx: S.Ctx, now: datetime | None = None, **kw: Any
) -> list[str]:
    """Start every enabled scheduled integration of the organisation that is due. Overlap is prevented by start_run."""
    from app.db.tenancy import scope_session

    now = now or utcnow()
    started: list[str] = []
    with factory() as db:
        scope_session(db, org_id)
        due = db.scalars(
            select(Integration).where(
                Integration.enabled.is_(True), Integration.schedule_minutes.is_not(None), Integration.next_run_at <= now
            )
        ).all()
        todo = []
        for integ in due:
            integ.next_run_at = now + timedelta(
                minutes=integ.schedule_minutes
            )  # advance first: a slow run cannot cause a stampede
            try:
                run = start_run(db, integ, "incremental", "scheduled", "scheduler", now=now)
            except ConflictError:
                continue
            todo.append(run.run_uid)
        db.commit()
    for uid in todo:
        execute_run(factory, cfg, org_id, uid, ctx, **kw)
        started.append(uid)
    return started
