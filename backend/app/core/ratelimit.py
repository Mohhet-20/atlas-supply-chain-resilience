"""In-process sliding-window rate limiter for the authentication endpoints.

It is per worker process and resets on restart; behind several workers each gets its own window, so the effective limit is
``workers x limit``. For a shared limit put a rate-limiting reverse proxy in front (documented in SECURE_DEPLOYMENT.md). The
client key is the socket peer address: ``X-Forwarded-For`` is NOT trusted, because a client can forge it to dodge the limit.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from app.core.errors import TooManyRequestsError


class SlidingWindow:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float = 60.0, now: float | None = None) -> None:
        t = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= t - window:
                q.popleft()
            if len(q) >= limit:
                raise TooManyRequestsError(
                    "Too many requests. Slow down and try again shortly.", int(window - (t - q[0])) + 1
                )
            q.append(t)
            if len(self._hits) > 10_000:  # bound memory: drop idle keys
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= t - window]:
                    self._hits.pop(k, None)


def client_of(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def auth_rate_limit(request: Request) -> None:
    """Dependency for /auth endpoints that need no credentials."""
    app = request.app
    if getattr(app.state, "_auth_limiter", None) is None:
        app.state._auth_limiter = SlidingWindow()
    limit = app.state.settings.auth_rate_limit_per_minute
    app.state._auth_limiter.hit(f"{client_of(request)}|{request.url.path}", limit)


_EXEMPT = ("/health", "/readiness")


class ApiRateLimitMiddleware:
    """Per-credential request ceiling for the whole API (in-process; per worker).

    The key is a short hash of the Authorization header (the raw token is never stored) or, for anonymous calls, the socket peer
    address. One noisy credential therefore cannot starve the others. It is NOT a distributed limit: behind N workers the
    effective ceiling is N x limit; use the reverse proxy for a global limit. Health probes are exempt.
    """

    def __init__(self, app, limit_per_minute: int, metrics=None) -> None:  # noqa: ANN001
        self.app, self.limit, self.metrics = app, limit_per_minute, metrics
        self.window = SlidingWindow()

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http" or self.limit <= 0 or scope["path"].endswith(_EXEMPT):
            await self.app(scope, receive, send)
            return
        import hashlib
        import json

        auth = dict(scope["headers"]).get(b"authorization", b"")
        peer = (scope.get("client") or ("unknown", 0))[0]
        key = "t:" + hashlib.sha256(auth).hexdigest()[:16] if auth else "ip:" + peer
        try:
            self.window.hit(key, self.limit)
        except TooManyRequestsError as e:
            if self.metrics is not None:
                self.metrics.rate_limited += 1
            rid = (scope.get("state") or {}).get("request_id")
            body = json.dumps({"error": {"code": "rate_limited", "message": e.message, "request_id": rid}}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", str(e.retry_after).encode()),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
