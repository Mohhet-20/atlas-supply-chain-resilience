"""Load the synthetic demo dataset into a MIGRATED database.

Usage (from repo root, venv active):  python scripts/seed_demo.py
Safe to run repeatedly. Never creates the schema, deletes, or overwrites records.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.errors import AtlasError  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.db.session import create_engine_from_settings  # noqa: E402
from app.services.seed import seed_demo_data  # noqa: E402


def main() -> int:
    settings = get_settings()
    setup_logging(settings.log_level)
    engine = create_engine_from_settings(settings)
    try:
        result = seed_demo_data(engine, settings)
    except AtlasError as exc:
        print(f"SEED FAILED [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    print("Demo seed complete (all records are SYNTHETIC_DEMO).")
    print(f"  created:  {result.created}")
    print(f"  existing: {result.skipped_existing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
