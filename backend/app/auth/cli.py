"""Operator command line: ``python -m app.auth.cli create-user``.

There are no default credentials. The password is read with ``getpass`` (never from argv, so it stays out of
shell history and process listings) or from the ``ATLAS_NEW_PASSWORD`` environment variable for automation.
The first user in a fresh deployment is created here and is a platform administrator of the default organisation.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from app.auth import service as svc
from app.core.auth import Actor, Role
from app.core.config import get_settings
from app.core.errors import AtlasError
from app.db.session import create_db_engine, make_session_factory
from app.registry import audit

CLI_ACTOR = Actor("cli-operator", Role.ADMIN, True, platform_admin=True)


def _read_password() -> str:
    env = os.environ.get("ATLAS_NEW_PASSWORD")
    if env:
        return env
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    return first


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.auth.cli", description="ATLAS operator tasks")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user", help="Create a user in the default organisation")
    c.add_argument("username")
    c.add_argument("--role", choices=svc.USER_ROLES, default="admin")
    c.add_argument("--platform-admin", action="store_true", help="Also grant organisation management")
    c.add_argument("--display-name")
    args = p.parse_args(argv)

    cfg = get_settings()
    engine = create_db_engine(cfg.resolved_database_url)
    factory = make_session_factory(engine)
    password = _read_password()
    with factory() as db:
        try:
            user = svc.create_user(
                db, cfg, None, username=args.username, password=password, role=args.role, org_id=1,
                display_name=args.display_name, platform_admin=args.platform_admin, must_change=False,
            )  # fmt: skip
            audit.record(
                db, CLI_ACTOR, "admin.user.created", target_type="user", target_id=user.user_uid,
                details={"role": args.role, "platform_admin": args.platform_admin, "via": "cli"},
            )  # fmt: skip
            db.commit()
        except AtlasError as e:
            db.rollback()
            print(f"Error: {e}", file=sys.stderr)
            return 1
    print(f"Created user {user.user_uid} ({user.username}, role {args.role}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
