"""Optional in-process worker: webhook delivery, scheduled syncs and export expiry. At-least-once; every effect is idempotent."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from sqlalchemy import select

from app.db.tenancy import scope_session
from app.integrations import exports, sync
from app.integrations import webhooks as W
from app.models.identity import Organization

log = logging.getLogger("atlas.integrations")


def tick_once(app: Any) -> dict[str, int]:
    """One pass over every active organisation, each in its own tenant-scoped transaction. A failure is logged and skipped."""
    from app.api.v1.alerting import build_ctx

    st = app.state
    out = {"deliveries": 0, "syncs": 0, "expired": 0}
    with st.session_factory() as db:
        org_ids = list(db.scalars(select(Organization.id).where(Organization.status == "active")))
    for org_id in org_ids:
        try:
            with st.session_factory() as db:
                scope_session(db, org_id)
                res = W.deliver_due(
                    db,
                    st.settings,
                    getattr(st, "webhook_sender", None) or W.HttpSender(),
                    resolve=getattr(st, "webhook_resolve", True),
                )
                db.commit()
                out["deliveries"] += sum(res.values())
            started = sync.run_due(st.session_factory, st.settings, org_id, build_ctx(app, org_id=org_id))
            out["syncs"] += len(started)
        except Exception:
            log.exception("integration worker failed for organisation %s", org_id)
    try:
        out["expired"] = exports.sweep_expired(st.session_factory, st.settings)
    except Exception:
        log.exception("export sweep failed")
    return out


def run_loop(app: Any, stop: threading.Event) -> None:
    interval = float(app.state.settings.alerting_tick_seconds)
    beats = app.state.__dict__.setdefault("worker_beats", {})
    beats["integrations"] = time.time()
    while not stop.wait(interval):
        tick_once(app)
        beats["integrations"] = time.time()  # heartbeat: read by /system/health; a stale value reports a stuck worker
