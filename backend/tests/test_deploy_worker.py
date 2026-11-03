"""Deployment: standalone worker process and embedded-worker switch."""

from __future__ import annotations

import threading

from app import worker
from tests.integrations_helpers import client_for


def test_standalone_worker_starts_beats_and_stops_on_signal(settings, engine):
    with client_for(settings, engine, alerting_tick_seconds=1, embedded_worker=False) as c:
        assert (
            not hasattr(c.app.state, "worker_beats") or not c.app.state.worker_beats
        )  # API process did not start workers
        stop = threading.Event()
        t = threading.Thread(target=worker.run, args=(stop, c.app))
        t.start()
        for _ in range(50):
            if len(getattr(c.app.state, "worker_beats", {})) == 2:
                break
            stop.wait(0.1)
        beats = dict(c.app.state.worker_beats)
        stop.set()
        t.join(timeout=15)
        assert not t.is_alive() and set(beats) == {"alerting", "integrations"}


def test_worker_refuses_to_run_with_the_tick_disabled(settings, engine):
    with client_for(settings, engine, alerting_tick_seconds=0) as c:
        assert worker.run(threading.Event(), c.app) == 2


def test_embedded_worker_is_on_by_default_and_can_be_disabled(settings, engine):
    with client_for(settings, engine, alerting_tick_seconds=1) as c:
        assert c.app.state.settings.embedded_worker is True
        assert len(c.app.state.worker_beats) >= 1
    with client_for(settings, engine, alerting_tick_seconds=1, embedded_worker=False) as c:
        assert not getattr(c.app.state, "worker_beats", {})
