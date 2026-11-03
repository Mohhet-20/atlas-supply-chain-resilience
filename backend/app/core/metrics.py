"""In-process metrics: request counters, latency histograms, in-flight gauge, free-form counters.

Labels are low-cardinality by construction: the *route template* (``/suppliers/{supplier_id}``), method and status class. Raw
paths, query strings, user names, tokens and tenant ids are never labels. Process-local; scrape every process in a
multi-process deployment (see docs/deployment.md).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started = time.time()
        self.requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self.latency: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * (len(BUCKETS_MS) + 1))
        self.latency_sum: dict[tuple[str, str], float] = defaultdict(float)
        self.counters: dict[str, int] = defaultdict(int)
        self.inflight = 0
        self.rate_limited = 0

    def observe(self, method: str, route: str, status: int, ms: float) -> None:
        with self._lock:
            self.requests[(method, route, f"{status // 100}xx")] += 1
            h = self.latency[(method, route)]
            for i, b in enumerate(BUCKETS_MS):
                if ms <= b:
                    h[i] += 1
                    break
            else:
                h[-1] += 1
            self.latency_sum[(method, route)] += ms

    def incr(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[name] += n

    def quantile(self, route_key: tuple[str, str], q: float) -> float | None:
        h = self.latency.get(route_key)
        total = sum(h) if h else 0
        if not total:
            return None
        target, acc = q * total, 0
        for i, c in enumerate(h):
            acc += c
            if acc >= target:
                return float(BUCKETS_MS[i]) if i < len(BUCKETS_MS) else float(BUCKETS_MS[-1])
        return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            routes: dict[str, Any] = {}
            for (m, r), h in self.latency.items():
                n = sum(h)
                routes[f"{m} {r}"] = {"count": n, "mean_ms": round(self.latency_sum[(m, r)] / n, 2) if n else None,
                                      "p50_le_ms": self.quantile((m, r), 0.5), "p95_le_ms": self.quantile((m, r), 0.95), "p99_le_ms": self.quantile((m, r), 0.99)}  # fmt: skip
            total = sum(self.requests.values())
            errors = sum(v for (_, _, c), v in self.requests.items() if c == "5xx")
            return {
                "uptime_seconds": round(time.time() - self.started), "requests_total": total, "server_errors_total": errors,
                "error_rate": round(errors / total, 4) if total else 0.0, "inflight": self.inflight, "rate_limited_total": self.rate_limited,
                "routes": routes, "counters": dict(self.counters),
            }  # fmt: skip

    def prometheus(self, extra: dict[str, float] | None = None) -> str:
        out = ["# TYPE atlas_http_requests_total counter"]
        with self._lock:
            for (m, r, c), v in sorted(self.requests.items()):
                out.append(f'atlas_http_requests_total{{method="{m}",route="{r}",status="{c}"}} {v}')
            out.append("# TYPE atlas_http_request_duration_ms histogram")
            for (m, r), h in sorted(self.latency.items()):
                acc = 0
                for i, b in enumerate(BUCKETS_MS):
                    acc += h[i]
                    out.append(f'atlas_http_request_duration_ms_bucket{{method="{m}",route="{r}",le="{b}"}} {acc}')
                acc += h[-1]
                out.append(f'atlas_http_request_duration_ms_bucket{{method="{m}",route="{r}",le="+Inf"}} {acc}')
                out.append(
                    f'atlas_http_request_duration_ms_sum{{method="{m}",route="{r}"}} {round(self.latency_sum[(m, r)], 2)}'
                )
                out.append(f'atlas_http_request_duration_ms_count{{method="{m}",route="{r}"}} {acc}')
            out.append("# TYPE atlas_inflight_requests gauge")
            out.append(f"atlas_inflight_requests {self.inflight}")
            out.append(f"atlas_rate_limited_total {self.rate_limited}")
            for name, v in sorted(self.counters.items()):
                out.append(f'atlas_events_total{{name="{name}"}} {v}')
        for k, v in (extra or {}).items():
            out.append(f"{k} {v}")
        return "\n".join(out) + "\n"


class MetricsMiddleware:
    """Outermost ASGI layer so it also sees rate-limited and oversized-body responses."""

    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app, self.metrics = app, metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = {"v": 500}
        t0 = time.perf_counter()

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["v"] = message["status"]
            await send(message)

        self.metrics.inflight += 1
        try:
            await self.app(scope, receive, wrapped)
        finally:
            self.metrics.inflight -= 1
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            self.metrics.observe(scope["method"], route, status["v"], (time.perf_counter() - t0) * 1000)
