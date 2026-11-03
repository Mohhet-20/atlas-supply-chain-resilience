#!/usr/bin/env python3
"""Validate ATLAS configuration before starting or deploying. Exit 0 = valid (warnings allowed), 1 = invalid.

Run from backend/ with the target environment exported (or an --env-file):
    PYTHONPATH=. python ../scripts/validate_config.py [--env-file ../.env.production] [--strict]

Secrets are never printed. --strict turns warnings into failures (use it for production releases); items starting with "note:" are informational and never fail.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def load_env_file(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def check(env: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Importable so tests can drive it with an explicit environment."""
    from pydantic import ValidationError

    from app.core.config import Settings

    errors: list[str] = []
    warnings: list[str] = []
    try:
        cfg = (
            Settings(_env_file=None, **{k[6:].lower(): v for k, v in (env or {}).items() if k.startswith("ATLAS_")})
            if env is not None
            else Settings()
        )
    except ValidationError as e:
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "settings"
            errors.append(f"{loc}: {err['msg']}")  # pydantic messages never include secret values for our validators
        return errors, warnings
    except ValueError as e:
        return [str(e)], warnings
    if cfg.production_like:
        url = cfg.resolved_database_url
        if url.startswith("sqlite"):
            warnings.append(
                "note: database is SQLite: single process only, no HA; PostgreSQL is untested (docs/deployment.md)"
            )
        if cfg.integrations_master_key is None:
            warnings.append("ATLAS_INTEGRATIONS_MASTER_KEY is unset: webhook creation will fail closed (503)")
        if cfg.api_tokens is None:
            warnings.append(
                "ATLAS_API_TOKENS is unset: access needs user accounts (create the first admin with app.auth.cli)"
            )
        if not cfg.write_api_allowed:
            warnings.append("note: write endpoints are disabled (ATLAS_WRITE_API_ENABLED unset in production)")
        if cfg.demo_data_allowed and cfg.environment == "production":
            warnings.append("ATLAS_DEMO_DATA_ENABLED=true in production: synthetic demo data may be seeded")
        if cfg.api_rate_limit_per_minute == 0:
            warnings.append("API rate limit is disabled (ATLAS_API_RATE_LIMIT_PER_MINUTE=0)")
        if cfg.alerting_tick_seconds <= 0:
            warnings.append(
                "in-process worker is disabled (ATLAS_ALERTING_TICK_SECONDS<=0): no alert evaluation, webhooks or scheduled syncs"
            )
    else:
        warnings.append(f"note: environment is '{cfg.environment}', not staging/production: production rules not applied")
    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", type=Path)
    p.add_argument("--strict", action="store_true")
    a = p.parse_args(argv)
    if a.env_file:
        load_env_file(a.env_file)
    errors, warnings = check()
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    failed = bool(errors) or (a.strict and any(not w.startswith("note:") for w in warnings))  # "note:" = known, accepted topology facts
    print("CONFIG " + ("INVALID" if failed else "VALID"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
