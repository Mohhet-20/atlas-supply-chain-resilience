"""Performance: the validated TTL cache - hit, version invalidation, TTL, tenant isolation, single flight, outage fallback, bounds."""

from __future__ import annotations

import threading
import time

import pytest

from app.core.cache import TTLCache
from app.models.enums import EntityStatus, Provenance
from app.models.supplier import Supplier
from tests.integrations_helpers import API, admin, client_for


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_hit_miss_and_version_invalidation():
    c, calls = TTLCache(), []

    def compute():
        calls.append(1)
        return len(calls)

    assert c.get_or_compute(("k", 1), "v1", compute) == 1
    assert c.get_or_compute(("k", 1), "v1", compute) == 1  # hit
    assert c.get_or_compute(("k", 1), "v2", compute) == 2  # version changed -> recomputed, stale value never served
    s = c.stats()
    assert (s["hits"], s["misses"], s["stale_discarded"]) == (1, 2, 1)


def test_ttl_expiry_with_injected_clock():
    clock = Clock()
    c = TTLCache(default_ttl=10, clock=clock)
    c.get_or_compute(("k",), "v", lambda: "a")
    clock.t += 9
    assert c.get_or_compute(("k",), "v", lambda: "b") == "a"
    clock.t += 2
    assert c.get_or_compute(("k",), "v", lambda: "b") == "b"


def test_keys_isolate_tenants_and_parameters():
    c = TTLCache()
    assert c.get_or_compute(("dashboard", 1), "v", lambda: "org1") == "org1"
    assert c.get_or_compute(("dashboard", 2), "v", lambda: "org2") == "org2"
    assert c.get_or_compute(("dashboard", 1), "v", lambda: "x") == "org1"


def test_lru_bound_and_invalidate():
    c = TTLCache(max_entries=8)
    for i in range(20):
        c.get_or_compute(("k", i), "v", lambda i=i: i)
    assert c.stats()["entries"] == 8 and c.stats()["evictions"] == 12
    assert c.invalidate(lambda k: k[1] < 100) == 8 and c.stats()["entries"] == 0


def test_errors_are_not_cached_and_disabled_cache_passes_through():
    c = TTLCache()
    with pytest.raises(ValueError):
        c.get_or_compute(("k",), "v", lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert c.get_or_compute(("k",), "v", lambda: "ok") == "ok"
    off = TTLCache(enabled=False)
    assert [off.get_or_compute(("k",), "v", lambda i=i: i) for i in range(3)] == [0, 1, 2]
    assert off.stats()["bypassed"] == 3


def test_cache_machinery_failure_falls_back_to_compute():
    c = TTLCache()

    def broken(*a, **k):
        raise RuntimeError("cache backend down")

    c._lookup = broken  # type: ignore[method-assign]
    assert c.get_or_compute(("k",), "v", lambda: "computed") == "computed"
    assert c.stats()["errors"] >= 1


def test_single_flight_computes_once_under_concurrency():
    c, calls, out = TTLCache(), [], []
    gate = threading.Barrier(8)

    def slow():
        calls.append(1)
        time.sleep(0.15)
        return "v"

    def worker():
        gate.wait()
        out.append(c.get_or_compute(("k",), "v", slow))

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out == ["v"] * 8 and len(calls) == 1


def test_dashboard_is_cached_and_invalidated_by_data_change(settings, engine):
    with client_for(settings, engine) as c:
        first = c.get(f"{API}/dashboard/summary", headers=admin())
        assert first.status_code == 200
        c.get(f"{API}/dashboard/summary", headers=admin())
        assert c.app.state.cache.stats()["hits"] >= 1
        n = first.json()["supplier_count"]
        with c.app.state.session_factory() as db:
            db.add(
                Supplier(
                    supplier_code="SYN-CACHE-PROBE",
                    name="Synthetic Cache Probe",
                    country_or_region="Synthetic",
                    category="Synthetic",
                    status=EntityStatus.ACTIVE,
                    provenance=Provenance.SYNTHETIC_DEMO,
                )
            )
            db.commit()
        assert c.get(f"{API}/dashboard/summary", headers=admin()).json()["supplier_count"] == n + 1
        assert c.app.state.cache.stats()["stale_discarded"] >= 1


def test_cache_disabled_by_config_still_serves(settings, engine):
    with client_for(settings, engine, cache_enabled=False) as c:
        assert c.get(f"{API}/dashboard/summary", headers=admin()).status_code == 200
        assert c.get(f"{API}/dashboard/summary", headers=admin()).status_code == 200
        assert c.app.state.cache.stats()["hits"] == 0
