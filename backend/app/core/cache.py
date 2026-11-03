"""Small, bounded, in-process cache with version validation and single-flight computation.

Design rules (see docs/architecture.md):
* A value is returned only if its *version token* still equals the caller's current token, so a stale value is never served
  after the underlying data changed, even if the TTL has not elapsed. The TTL is a second line of defence.
* Keys are tuples built by the caller and MUST contain the organisation id; there is no implicit global namespace.
* Bounded: LRU with ``max_entries``; expired entries are dropped on access.
* Single-flight: concurrent misses for one key compute once; the others wait and reuse the result.
* Fail-open: any error inside the cache machinery falls back to computing directly (never to an error response).
* Process-local. With several processes each has its own copy; the version token keeps every copy correct.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

_MISS = object()
_STRIPES = 64


class TTLCache:
    def __init__(
        self,
        max_entries: int = 256,
        default_ttl: float = 60.0,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_entries, self.default_ttl, self.enabled, self._clock = max_entries, default_ttl, enabled, clock
        self._data: OrderedDict[tuple, tuple[float, Any, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._flight = [threading.Lock() for _ in range(_STRIPES)]
        self.hits = self.misses = self.evictions = self.errors = self.bypassed = self.stale = 0

    # -- internals --------------------------------------------------------------------------------------------------
    def _lookup(self, key: tuple, version: Any) -> Any:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return _MISS
            expires, ver, value = item
            if expires <= self._clock():
                del self._data[key]
                return _MISS
            if ver != version:
                self.stale += 1
                del self._data[key]
                return _MISS
            self._data.move_to_end(key)
            return value

    def _store(self, key: tuple, version: Any, value: Any, ttl: float) -> None:
        with self._lock:
            self._data[key] = (self._clock() + ttl, version, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)
                self.evictions += 1

    # -- public -----------------------------------------------------------------------------------------------------
    def get_or_compute(self, key: tuple, version: Any, compute: Callable[[], Any], ttl: float | None = None) -> Any:
        if not self.enabled:
            self.bypassed += 1
            return compute()
        try:
            hit = self._lookup(key, version)
        except Exception:
            self.errors += 1
            return compute()
        if hit is not _MISS:
            self.hits += 1
            return hit
        with self._flight[hash(key) % _STRIPES]:
            try:
                hit = self._lookup(key, version)
            except Exception:
                self.errors += 1
                return compute()
            if hit is not _MISS:
                self.hits += 1
                return hit
            self.misses += 1
            value = compute()  # errors from compute propagate and are never cached
            try:
                self._store(key, version, value, self.default_ttl if ttl is None else ttl)
            except Exception:
                self.errors += 1
            return value

    def invalidate(self, predicate: Callable[[tuple], bool] | None = None) -> int:
        with self._lock:
            keys = [k for k in self._data if predicate is None or predicate(k)]
            for k in keys:
                del self._data[k]
            return len(keys)

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled, "entries": len(self._data), "max_entries": self.max_entries, "hits": self.hits, "misses": self.misses,
            "stale_discarded": self.stale, "evictions": self.evictions, "errors": self.errors, "bypassed": self.bypassed,
            "hit_rate": round(self.hits / total, 3) if total else None,
        }  # fmt: skip
