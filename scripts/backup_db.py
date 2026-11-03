#!/usr/bin/env python3
"""Online SQLite backup, verification and restore for ATLAS.

  python scripts/backup_db.py backup  --db data/atlas.db --out backups/atlas-YYYYmmdd.db
  python scripts/backup_db.py verify  --file backups/atlas-YYYYmmdd.db
  python scripts/backup_db.py restore --file backups/atlas-YYYYmmdd.db --db data/atlas.db   (refuses to overwrite without --force)

Uses SQLite's online backup API, so it is consistent while the application is running (no file copy of a live database).
It covers the database only; exported files and the secrets referenced by ``env:NAME`` credentials are NOT included.
Postgres deployments use pg_dump / PITR instead (see docs/deployment.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path


def backup(src: Path, dst: Path) -> dict:
    if not src.exists():
        raise FileNotFoundError(f"database not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".partial")
    tmp.unlink(missing_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(tmp)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    report = verify(tmp)
    tmp.replace(dst)  # a backup only appears under its real name once it has verified
    report["sha256"] = hashlib.sha256(dst.read_bytes()).hexdigest()
    return report


def verify(path: Path) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        tables = [
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        ]
        counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in sorted(tables)}
        rev = con.execute("SELECT version_num FROM alembic_version").fetchone() if "alembic_version" in tables else None
    finally:
        con.close()
    ok = integrity == "ok" and not fk
    if not ok:
        raise RuntimeError(f"backup failed verification: integrity={integrity!r}, foreign_key_violations={len(fk)}")
    return {"integrity": integrity, "revision": rev[0] if rev else None, "tables": len(counts), "row_counts": counts}


def restore(file: Path, db: Path, force: bool = False) -> dict:
    report = verify(file)  # never restore something that does not verify
    if db.exists() and not force:
        raise FileExistsError(f"{db} exists; stop the application and pass --force to replace it")
    db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    s = sqlite3.connect(f"file:{file}?mode=ro", uri=True)
    d = sqlite3.connect(db)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    after = verify(db)
    if after["row_counts"] != report["row_counts"]:
        raise RuntimeError("restored database does not match the backup")
    return after


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--db", required=True, type=Path)
    b.add_argument("--out", required=True, type=Path)
    v = sub.add_parser("verify")
    v.add_argument("--file", required=True, type=Path)
    r = sub.add_parser("restore")
    r.add_argument("--file", required=True, type=Path)
    r.add_argument("--db", required=True, type=Path)
    r.add_argument("--force", action="store_true")
    a = p.parse_args(argv)
    try:
        out = (
            backup(a.db, a.out)
            if a.cmd == "backup"
            else verify(a.file)
            if a.cmd == "verify"
            else restore(a.file, a.db, a.force)
        )
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
