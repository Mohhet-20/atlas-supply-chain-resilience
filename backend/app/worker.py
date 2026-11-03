"""Standalone background worker: ``python -m app.worker``.

Runs the same alert-evaluation tick and integration worker (webhook delivery, scheduled syncs, export expiry) that the API process
can run in-process, but without serving HTTP. Set ``ATLAS_EMBEDDED_WORKER=false`` on the API process when this runs.

* Run exactly ONE worker process per database. Delivery and export claims are atomic and syncs have a database-enforced single active
  run, but the alert tick and export sweep have no lease (duplicate runs are harmless but wasteful).
* SIGTERM/SIGINT stop the loops after the current pass; work interrupted mid-delivery is recovered by lease expiry (at least once).
* Needs ``ATLAS_ALERTING_TICK_SECONDS`` > 0 (it is the loop interval) and the same database and secrets as the API.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from app.api.v1.alerting import run_tick_loop
from app.core.config import get_settings
from app.integrations.worker import run_loop as run_integration_loop
from app.main import create_app

log = logging.getLogger("atlas.worker")


def run(stop: threading.Event | None = None, app=None) -> int:  # noqa: ANN001
    app = app or create_app(get_settings())
    settings = app.state.settings
    if settings.alerting_tick_seconds <= 0:
        log.error("ATLAS_ALERTING_TICK_SECONDS must be > 0 for the worker; nothing to do")
        return 2
    stop = stop or threading.Event()
    threads = [
        threading.Thread(target=run_tick_loop, args=(app, stop), name="atlas-alert-tick", daemon=True),
        threading.Thread(target=run_integration_loop, args=(app, stop), name="atlas-integrations", daemon=True),
    ]
    for t in threads:
        t.start()
    log.info("worker started (interval %ss)", settings.alerting_tick_seconds)
    while not stop.wait(1.0):
        if not all(t.is_alive() for t in threads):
            log.error("a worker thread died; exiting so the supervisor restarts the process")
            stop.set()
            for t in threads:
                t.join(timeout=5)
            return 1
    for t in threads:
        t.join(timeout=10)
    log.info("worker stopped")
    return 0


def main() -> int:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    return run(stop)


if __name__ == "__main__":
    sys.exit(main())
