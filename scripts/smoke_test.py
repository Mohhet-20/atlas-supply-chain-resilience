#!/usr/bin/env python3
"""Post-deployment smoke test. Read-only: no data is created, changed or deleted. Exit 0 only if every check passes.

    python scripts/smoke_test.py --base-url http://127.0.0.1:8000 [--token <bearer>] [--other-token <bearer of another org>]

--token is a session or API token of a user who can read data (viewer is enough). Without it only the public checks run
and the authenticated checks are reported as SKIPPED (a skip is not a pass). The token is sent only to --base-url and is never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

API = "/api/v1"


def call(base: str, path: str, token: str | None = None, timeout: float = 10.0) -> tuple[int, object]:
    req = urllib.request.Request(base + API + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - base URL is operator supplied
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}"
    try:
        return code, json.loads(raw or b"null")
    except ValueError:
        return code, None


def run(base: str, token: str | None, other_token: str | None) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []

    def rec(name: str, ok: bool | None, detail: str = "") -> None:
        results.append((name, "PASS" if ok else "SKIP" if ok is None else "FAIL", detail))

    c, b = call(base, "/health")
    rec("backend liveness", c == 200 and isinstance(b, dict) and b.get("status") == "ok", f"HTTP {c}")
    c, b = call(base, "/readiness")
    rec("database + schema readiness", c == 200 and isinstance(b, dict) and b.get("status") == "ready", f"HTTP {c}")
    c, _ = call(base, "/dashboard/summary")
    rec("unauthenticated access is refused", c in (401, 403, 503) if c else False, f"HTTP {c}")
    c, _ = call(base, "/dashboard/summary", "atl_s_" + "0" * 40)
    rec("invalid token is refused", c == 401, f"HTTP {c}")
    if not token:
        for n in (
            "authenticated profile",
            "dashboard",
            "suppliers",
            "inventory",
            "risk assessments",
            "alerts",
            "integrations subsystem",
            "file storage / exports",
            "analytical workflow availability",
            "worker health",
        ):
            rec(n, None, "no --token supplied")
        return results
    c, b = call(base, "/registry/me", token)
    rec("authenticated profile", c == 200, f"HTTP {c}")
    t0 = time.perf_counter()
    c, b = call(base, "/dashboard/summary", token, 30)
    rec(
        "dashboard loads",
        c == 200 and isinstance(b, dict) and "supplier_count" in b,
        f"HTTP {c}, {int((time.perf_counter() - t0) * 1000)} ms",
    )
    for name, path in [
        ("suppliers", "/suppliers?limit=1"),
        ("inventory", "/inventory/positions?limit=1"),
        ("risk assessments", "/risk/runs?limit=1"),
        ("alerts", "/alerting/alerts?limit=1"),
    ]:
        c, _ = call(base, path, token)
        rec(f"{name} retrieval", c in (200,), f"HTTP {c}")
    c, b = call(base, "/integrations/overview", token)
    rec(
        "integrations subsystem", c in (200, 403), f"HTTP {c}" + (" (token lacks integration.read)" if c == 403 else "")
    )
    c, b = call(base, "/exports", token)
    rec("export storage listing", c in (200, 403), f"HTTP {c}")
    c, b = call(base, "/analytics/overview", token, 30)
    rec("analytical workflow availability", c == 200, f"HTTP {c}")
    c, b = call(base, "/system/health", token)
    rec(
        "worker health (needs platform admin)",
        True if c == 200 and isinstance(b, dict) and b.get("status") in ("ok",) else (None if c == 403 else False),
        f"HTTP {c}" + (f", status {b.get('status')}" if isinstance(b, dict) else ""),
    )
    if other_token:
        c1, b1 = call(base, "/alerting/alerts?limit=100", token)
        c2, b2 = call(base, "/alerting/alerts?limit=100", other_token)
        ids1 = {a["alert_uid"] for a in (b1 or {}).get("items", [])} if c1 == 200 and isinstance(b1, dict) else None
        ids2 = {a["alert_uid"] for a in (b2 or {}).get("items", [])} if c2 == 200 and isinstance(b2, dict) else None
        rec(
            "tenant isolation (alert sets disjoint)",
            None if ids1 is None or ids2 is None else not (ids1 & ids2),
            f"HTTP {c1}/{c2}",
        )
    else:
        rec("tenant isolation", None, "no --other-token supplied")
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", required=True)
    p.add_argument("--token")
    p.add_argument("--other-token")
    a = p.parse_args(argv)
    results = run(a.base_url.rstrip("/"), a.token, a.other_token)
    w = max(len(n) for n, _, _ in results)
    for n, s, d in results:
        print(f"{s:5} {n:<{w}}  {d}")
    fails = sum(1 for _, s, _ in results if s == "FAIL")
    skips = sum(1 for _, s, _ in results if s == "SKIP")
    print(
        f"SMOKE {'FAILED' if fails else 'PASSED'}: {sum(1 for _, s, _ in results if s == 'PASS')} passed, {fails} failed, {skips} skipped"
    )
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
