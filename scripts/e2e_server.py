"""Start a throwaway ATLAS API for browser end-to-end tests and screenshots.

Usage (repo root, backend venv active):
    python scripts/e2e_server.py [--port 8765] [--frontend-origin http://127.0.0.1:4180]

It rebuilds data/e2e/atlas_e2e.db with scripts/demo_world.py (synthetic data only), creates one platform
administrator through the operator CLI, then serves the API with uvicorn until stopped. The user name and password
come from ATLAS_E2E_USERNAME / ATLAS_E2E_PASSWORD (synthetic defaults below, valid only for that throwaway file).
Nothing else is touched; the script refuses to run if ATLAS_ENVIRONMENT is production-like.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # noqa: S404 - runs this repository's own scripts with fixed arguments
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_USER = "e2e-admin"
DEFAULT_PASSWORD = "Synthetic-E2E-Pass-Phrase-41!"  # noqa: S105 - synthetic, local throwaway database only


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--frontend-origin", default="http://127.0.0.1:4180")
    args = ap.parse_args()
    if os.environ.get("ATLAS_ENVIRONMENT", "development") in ("staging", "production"):
        print("Refusing to start the E2E server in a production-like environment.", file=sys.stderr)
        return 2

    db = ROOT / "data" / "e2e" / "atlas_e2e.db"
    py = sys.executable
    subprocess.run([py, str(ROOT / "scripts" / "demo_world.py"), "--db", str(db), "--reset"], check=True)  # noqa: S603

    env = {
        **os.environ,
        "ATLAS_ENVIRONMENT": "development",
        "ATLAS_DATABASE_URL": f"sqlite:///{db.as_posix()}",
        "ATLAS_DATA_DIR": str(db.parent),
        "ATLAS_CORS_ALLOWED_ORIGINS": f"{args.frontend_origin},{args.frontend_origin.replace('127.0.0.1', 'localhost')}",
        "ATLAS_LOG_LEVEL": "WARNING",
        "ATLAS_NEW_PASSWORD": os.environ.get("ATLAS_E2E_PASSWORD", DEFAULT_PASSWORD),
    }
    backend = ROOT / "backend"
    user = os.environ.get("ATLAS_E2E_USERNAME", DEFAULT_USER)
    subprocess.run(  # noqa: S603
        [py, "-m", "app.auth.cli", "create-user", user, "--role", "admin", "--platform-admin"],
        cwd=backend, env=env, check=True,
    )  # fmt: skip
    env.pop("ATLAS_NEW_PASSWORD")
    cmd = [py, "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1", "--port", str(args.port)]
    return subprocess.call(cmd, cwd=backend, env=env)  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())
