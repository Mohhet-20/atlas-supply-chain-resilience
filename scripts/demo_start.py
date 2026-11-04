"""Start the hosted public demo: build the synthetic demo world, create its accounts, then serve the API.

Used by the Render blueprint (render.yaml) through deploy/demo/Dockerfile. It runs in ATLAS_ENVIRONMENT=staging, so
the strict production checks apply (https CORS origin, security headers, no unauthenticated access, no /docs).

Accounts it creates (no password is ever shown to visitors):
    guest                                      the account behind the sign-in page's "Explore the live demo" button
                                               (POST /auth/guest). Role "editor": can run scenarios and change data,
                                               cannot administer. Its password is random and discarded.
    ATLAS_ADMIN_USERNAME / ATLAS_ADMIN_PASSWORD  the owner's private administrator, signed in with the normal form.
                                               Optional; set both as secrets in the hosting dashboard.
    PORT                                       the port to listen on (Render sets it)

The database is rebuilt from scratch on every start, so the demo resets itself on each deploy or restart.
"""

from __future__ import annotations

import os
import secrets
import subprocess  # noqa: S404 - runs this repository's own scripts with fixed arguments
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def create_user(py: str, env: dict[str, str], username: str, password: str, *extra: str) -> None:
    subprocess.run(  # noqa: S603
        [py, "-m", "app.auth.cli", "create-user", username, *extra],
        cwd=ROOT / "backend", env={**env, "ATLAS_NEW_PASSWORD": password}, check=True,
    )  # fmt: skip


def main() -> int:
    data_dir = Path(os.environ.get("ATLAS_DATA_DIR", "/data"))
    db = data_dir / "atlas_demo.db"
    py = sys.executable
    subprocess.run([py, str(ROOT / "scripts" / "demo_world.py"), "--db", str(db), "--reset"], check=True)  # noqa: S603

    env = {
        **os.environ,
        "ATLAS_DATABASE_URL": f"sqlite:///{db.as_posix()}",
        "ATLAS_DATA_DIR": str(data_dir),
        "ATLAS_GUEST_ACCESS_ENABLED": "true",
        "ATLAS_GUEST_USERNAME": "guest",
    }
    # Nobody signs in to the guest account with a password, so it gets one nobody knows.
    create_user(py, env, "guest", secrets.token_urlsafe(32), "--role", "editor", "--display-name", "Guest")

    admin_user = os.environ.get("ATLAS_ADMIN_USERNAME", "").strip()
    admin_password = os.environ.get("ATLAS_ADMIN_PASSWORD", "")
    if admin_user and admin_password:
        owner = ("--role", "admin", "--platform-admin", "--display-name", "Owner")
        create_user(py, env, admin_user, admin_password, *owner)
    else:
        print("ATLAS_ADMIN_USERNAME/ATLAS_ADMIN_PASSWORD not set: no administrator account this run.", file=sys.stderr)

    port = os.environ.get("PORT", "8000")
    host = "0.0.0.0"  # noqa: S104 - inside the container; Render routes traffic to it
    cmd = [py, "-m", "uvicorn", "app.main:create_app", "--factory", "--host", host, "--port", port, "--workers", "1"]
    os.chdir(ROOT / "backend")
    os.execvpe(cmd[0], cmd, env)  # noqa: S606 - replaces this process so signals reach uvicorn directly
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
