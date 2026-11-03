"""Start the hosted public demo: build the synthetic demo world, create the demo account, then serve the API.

Used by the Render blueprint (render.yaml) through deploy/demo/Dockerfile. It runs in ATLAS_ENVIRONMENT=staging, so
the strict production checks apply (https CORS origin, security headers, no unauthenticated admin, no /docs).

    ATLAS_DEMO_USERNAME / ATLAS_DEMO_PASSWORD   the shared demo account (public by design; synthetic data only)
    ATLAS_DEMO_ROLE                             its role, default "editor" (can run scenarios, cannot administer)
    PORT                                        the port to listen on (Render sets it)

The database is rebuilt from scratch on every start, so the demo resets itself on each deploy or restart.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - runs this repository's own scripts with fixed arguments
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    data_dir = Path(os.environ.get("ATLAS_DATA_DIR", "/data"))
    db = data_dir / "atlas_demo.db"
    py = sys.executable
    subprocess.run([py, str(ROOT / "scripts" / "demo_world.py"), "--db", str(db), "--reset"], check=True)  # noqa: S603

    env = {**os.environ, "ATLAS_DATABASE_URL": f"sqlite:///{db.as_posix()}", "ATLAS_DATA_DIR": str(data_dir)}
    user = os.environ.get("ATLAS_DEMO_USERNAME", "demo")
    password = os.environ.get("ATLAS_DEMO_PASSWORD")
    if not password:
        print("ATLAS_DEMO_PASSWORD is not set; refusing to start the public demo.", file=sys.stderr)
        return 2
    role = os.environ.get("ATLAS_DEMO_ROLE", "editor")
    subprocess.run(  # noqa: S603
        [py, "-m", "app.auth.cli", "create-user", user, "--role", role, "--display-name", "Demo visitor"],
        cwd=ROOT / "backend", env={**env, "ATLAS_NEW_PASSWORD": password}, check=True,
    )  # fmt: skip
    port = os.environ.get("PORT", "8000")
    host = "0.0.0.0"  # noqa: S104 - inside the container; Render routes traffic to it
    cmd = [py, "-m", "uvicorn", "app.main:create_app", "--factory", "--host", host, "--port", port, "--workers", "1"]
    os.chdir(ROOT / "backend")
    os.execvpe(cmd[0], cmd, env)  # noqa: S606 - replaces this process so signals reach uvicorn directly
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
