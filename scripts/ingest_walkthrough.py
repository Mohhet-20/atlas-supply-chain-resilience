"""Executes the ingestion workflow end to end against a throw-away database and writes a Markdown transcript.

Usage (repo root, backend venv active):
    python scripts/ingest_walkthrough.py [--output README.md]

It runs the real FastAPI app in-process (no network, nothing persisted outside a temp dir), so every response
shown in the transcript is genuine output of this code base.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from alembic import command  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.db.migrations_check import alembic_config  # noqa: E402
from app.db.session import create_db_engine  # noqa: E402
from app.main import create_app  # noqa: E402

SAMPLES = ROOT / "data" / "samples"
P = "/api/v1"
OUT: list[str] = []


def say(text: str = "") -> None:
    OUT.append(text)


def block(title: str, body: str, lang: str = "json") -> None:
    say(f"```{lang}")
    say(body.rstrip())
    say("```")
    say()


def trim(obj, keys):
    return {k: obj[k] for k in keys if k in obj}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(ROOT / "docs" / "ingestion" / "WALKTHROUGH.md"))
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{(Path(tmp) / 'walkthrough.db').as_posix()}"
        command.upgrade(alembic_config(url), "head")
        engine = create_db_engine(url)
        settings = Settings(_env_file=None, environment="test", database_url=url, data_dir=Path(tmp), log_level="ERROR")
        with TestClient(create_app(settings, engine)) as c:
            run(c)
        engine.dispose()

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text("\n".join(OUT) + "\n", encoding="utf-8")
    print(f"Wrote {args.output} ({len(OUT)} lines)")
    return 0


def post_file(c, path, name, entity, **params):
    data = (SAMPLES / name).read_bytes()
    fmt = "JSON" if name.endswith(".json") else "CSV"
    ctype = "application/json" if fmt == "JSON" else "text/csv"
    q = {"entity_type": entity, "filename": name, **params}
    return c.post(f"{P}{path}", params=q, content=data, headers={"Content-Type": ctype})


def count(c, what):
    return c.get(f"{P}/{what}", params={"limit": 1}).json()["total"]


def step(n, title, explanation):
    say(f"## {n}. {title}")
    say()
    say(explanation)
    say()


def run(c: TestClient) -> None:
    say("# ATLAS ingestion walkthrough (executed)")
    say()
    say(
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `scripts/ingest_walkthrough.py` against a throw-away "
        "SQLite database created with `alembic upgrade head`. Every response below is real output of the API. "
        "All sample data is fictional (see `data/samples/README.md`)."
    )
    say()

    step(1, "Register a data source", "A source's namespace scopes every external identifier it supplies.")
    r = c.post(f"{P}/ingestion/sources", json={"namespace": "sample-erp", "name": "Sample ERP (synthetic)"})
    src = r.json()
    block(
        "source",
        f"POST /ingestion/sources -> {r.status_code}\n"
        + json.dumps(trim(src, ["id", "namespace", "name", "provenance"]), indent=2),
    )

    step(
        2,
        "Preview a file with problems (nothing is stored)",
        "`suppliers_with_errors.csv` contains a missing name, a bad identifier, an unsupported status, a duplicate id, "
        "a short row and a formula-looking id. Preview runs the same pipeline as commit and persists nothing.",
    )
    r = post_file(
        c, "/ingestion/preview", "suppliers_with_errors.csv", "SUPPLIER", source_id=src["id"], policy="PARTIAL"
    )
    pv = r.json()
    block(
        "preview",
        f"POST /ingestion/preview -> {r.status_code}\n"
        + json.dumps(
            {
                "persisted": pv["persisted"],
                "counts": pv["counts"],
                "finding_counts": pv["finding_counts"],
                "would_apply": pv["would_apply"],
            },
            indent=2,
        ),
    )
    say("| Row | Line | Rule | Severity | Field | Explanation |")
    say("|---|---|---|---|---|---|")
    for f in pv["findings"]:
        if f["severity"] in ("ERROR", "CRITICAL"):
            say(
                f"| {f['row_index']} | {f['source_line']} | {f['rule_id']} | {f['severity']} | {f['field'] or ''} | {f['message'].replace('|', '/')} |"
            )
    say()
    say(
        f"Suppliers stored after preview: **{count(c, 'suppliers')}**; batches recorded: **{c.get(f'{P}/ingestion/batches').json()['total']}**."
    )
    say()

    step(
        3,
        "Commit with the ATOMIC (all-or-nothing) policy",
        "Any blocking finding rejects the whole batch. The batch and its findings are kept for audit; no supplier is created.",
    )
    r = post_file(c, "/ingestion/commit", "suppliers_with_errors.csv", "SUPPLIER", source_id=src["id"], policy="ATOMIC")
    b = r.json()
    block(
        "atomic",
        f"POST /ingestion/commit?policy=ATOMIC -> HTTP {r.status_code} (ok={b['ok']})\n"
        + json.dumps(
            trim(
                b["batch"],
                [
                    "batch_uid",
                    "status",
                    "created_count",
                    "rejected_count",
                    "skipped_count",
                    "duplicate_count",
                    "failure_reason",
                ],
            ),
            indent=2,
        ),
    )
    say(f"Suppliers stored: **{count(c, 'suppliers')}**.")
    say()

    step(
        4,
        "Commit the same file with the PARTIAL policy",
        "Valid rows are applied; each invalid row is rejected with its original row/line number. Committed, rejected, skipped and duplicate counts are separate.",
    )
    r = post_file(
        c, "/ingestion/commit", "suppliers_with_errors.csv", "SUPPLIER", source_id=src["id"], policy="PARTIAL"
    )
    b = r.json()
    block(
        "partial",
        f"HTTP {r.status_code}\n"
        + json.dumps(
            trim(
                b["batch"],
                [
                    "batch_uid",
                    "status",
                    "total_rows",
                    "created_count",
                    "rejected_count",
                    "duplicate_count",
                    "skipped_count",
                    "finding_count",
                ],
            ),
            indent=2,
        ),
    )
    rows = c.get(f"{P}/ingestion/batches/{b['batch']['id']}/rows").json()["items"]
    say("| Row | Line | External id | Outcome | Detail |")
    say("|---|---|---|---|---|")
    for x in rows:
        say(
            f"| {x['row_index']} | {x['source_line']} | {x['external_id'] or ''} | {x['outcome']} | {(x['message'] or '').replace('|', '/')} |"
        )
    say()

    step(
        5,
        "Commit a clean file, then retry it (idempotency)",
        "Re-sending identical input returns the already-applied batch (`replay: true`, HTTP 200) and creates nothing.",
    )
    r1 = post_file(c, "/ingestion/commit", "suppliers_valid.csv", "SUPPLIER", source_id=src["id"])
    r2 = post_file(c, "/ingestion/commit", "suppliers_valid.csv", "SUPPLIER", source_id=src["id"])
    block(
        "idempotent",
        f"first : HTTP {r1.status_code} replay={r1.json()['replay']} status={r1.json()['batch']['status']} created={r1.json()['batch']['created_count']}\n"
        f"second: HTTP {r2.status_code} replay={r2.json()['replay']} batch={r2.json()['batch']['batch_uid'] == r1.json()['batch']['batch_uid']} (same batch)",
        "text",
    )
    say(f"Suppliers stored: **{count(c, 'suppliers')}** (no duplicates from the retry).")
    say()

    step(
        6,
        "Use a mapping profile for a foreign-format export",
        "`suppliers_erp_export.csv` is semicolon-delimited with German headers and `J`/`N` flags. A versioned mapping profile translates it. Profiles are drafts until activated and immutable afterwards.",
    )
    prof = json.loads((SAMPLES / "mapping_erp_suppliers.json").read_text())
    pr = c.post(f"{P}/ingestion/mapping-profiles", json={**prof, "source_id": src["id"]}).json()
    act = c.post(f"{P}/ingestion/mapping-profiles/{pr['id']}/activate").json()
    block(
        "profile",
        json.dumps(
            {"id": act["id"], "name": act["name"], "version": act["version"], "status": act["status"]}, indent=2
        ),
    )
    r = post_file(
        c,
        "/ingestion/commit",
        "suppliers_erp_export.csv",
        "SUPPLIER",
        source_id=src["id"],
        mapping_profile_id=pr["id"],
        delimiter=";",
    )
    b = r.json()["batch"]
    block(
        "mapped",
        f"HTTP {r.status_code} status={b['status']} created={b['created_count']} profile={b['mapping_profile_ref']}",
        "text",
    )
    erp = c.get(f"{P}/suppliers", params={"q": "ERP Supplier"}).json()["items"]
    say(
        "Resulting records: "
        + ", ".join(f"`{s['source_namespace']}:{s['external_id']}` {s['name']} ({s['status']})" for s in erp)
    )
    say()

    step(
        7,
        "Unit conversion and rejection of unknown units",
        "The facility profile converts `units/day` to `UNITS/WEEK` (x7). `pallets` has no declared factor, so that row is rejected instead of guessed.",
    )
    fprof = json.loads((SAMPLES / "mapping_facility_units.json").read_text())
    fp = c.post(f"{P}/ingestion/mapping-profiles", json={**fprof, "source_id": src["id"]}).json()
    c.post(f"{P}/ingestion/mapping-profiles/{fp['id']}/activate")
    r = post_file(
        c, "/ingestion/commit", "facilities_units.csv", "FACILITY", source_id=src["id"], mapping_profile_id=fp["id"]
    )
    b = r.json()["batch"]
    block(
        "units",
        f"HTTP {r.status_code} status={b['status']} created={b['created_count']} rejected={b['rejected_count']}",
        "text",
    )
    for f in c.get(f"{P}/facilities", params={"q": "Synthetic"}).json()["items"]:
        say(f"- `{f['external_id']}` capacity **{f['capacity_value']} {f['capacity_unit']}**")
    say()

    step(
        8,
        "Import the rest of the network in dependency order",
        "Facilities (with an owning-supplier reference and coordinates only where known), products, relationships, then product links.",
    )
    for name, ent in (
        ("facilities.csv", "FACILITY"),
        ("products.csv", "PRODUCT"),
        ("relationships.csv", "RELATIONSHIP"),
        ("product_links.csv", "PRODUCT_LINK"),
    ):
        r = post_file(c, "/ingestion/commit", name, ent, source_id=src["id"])
        b = r.json()["batch"]
        say(
            f"- `{name}` ({ent}): HTTP {r.status_code}, **{b['status']}**, created {b['created_count']}, rejected {b['rejected_count']}"
        )
    say()
    f2 = next(f for f in c.get(f"{P}/facilities", params={"q": "Sample Warehouse"}).json()["items"])
    f1 = next(f for f in c.get(f"{P}/facilities", params={"q": "Sample Plant"}).json()["items"])
    say(
        f"Coordinates are never invented: `{f1['external_id']}` -> ({f1['latitude']}, {f1['longitude']}) `{f1['location_status']}`; "
        f"`{f2['external_id']}` -> ({f2['latitude']}, {f2['longitude']}) `{f2['location_status']}`. Owner of the plant: **{f1['owner']['name']}**."
    )
    say()

    step(
        9,
        "Update an existing record (and see lineage)",
        "Re-importing a changed row updates only the fields supplied (blank cells never erase data) and appends a lineage entry.",
    )
    changed = (
        (SAMPLES / "suppliers_valid.csv")
        .read_text()
        .replace("Synthetic Sample Supplier Two", "Synthetic Sample Supplier Two (renamed)")
    )
    r = c.post(
        f"{P}/ingestion/commit",
        params={"source_id": src["id"], "entity_type": "SUPPLIER", "filename": "suppliers_valid_v2.csv"},
        content=changed.encode(),
        headers={"Content-Type": "text/csv"},
    )
    b = r.json()["batch"]
    say(f"HTTP {r.status_code}: updated **{b['updated_count']}**, unchanged **{b['unchanged_count']}**.")
    say()
    sup = c.get(f"{P}/suppliers", params={"q": "Supplier Two"}).json()["items"][0]
    lin = c.get(f"{P}/lineage/SUPPLIER/{sup['id']}").json()["items"]
    say("| When (UTC) | Outcome | Source id | File / row | Mapping | Changed fields |")
    say("|---|---|---|---|---|---|")
    for ln in reversed(lin):
        say(
            f"| {ln['ingested_at'][:19]} | {ln['outcome']} | `{ln['source_namespace']}:{ln['external_id']}` | {ln['file_name']} row {ln['row_index']} | {ln['mapping_profile_ref']} | {', '.join(ln['changed_fields'] or [])} |"
        )
    say()

    step(10, "JSON input", 'A `{"records": [...]}` document; nested objects would flatten to dotted keys.')
    r = post_file(c, "/ingestion/commit", "suppliers.json", "SUPPLIER", source_id=src["id"])
    say(f"HTTP {r.status_code}: **{r.json()['batch']['status']}**, created {r.json()['batch']['created_count']}.")
    say()

    step(
        11,
        "Review data-quality findings and export them",
        "Findings are persisted, queryable and reviewable; review changes only the resolution status. The CSV export neutralizes spreadsheet formulas.",
    )
    fl = c.get(f"{P}/ingestion/findings", params={"severity": "ERROR", "limit": 3}).json()
    say(
        f"Open ERROR findings: **{fl['total']}**. First one: `{fl['items'][0]['rule_id']}` — {fl['items'][0]['message']}"
    )
    say()
    rv = c.put(
        f"{P}/ingestion/findings/{fl['items'][0]['id']}/resolution",
        json={"resolution_status": "ACKNOWLEDGED", "note": "Known bad sample row"},
    ).json()
    say(f"Reviewed finding {rv['id']}: `{rv['resolution_status']}` (the finding itself is retained).")
    say()
    csv_text = c.get(f"{P}/ingestion/findings/export.csv", params={"rule_id": "DQ002"}).text
    block("csv", "\n".join(csv_text.splitlines()[:3]), "text")
    say(
        "Note the leading `'` before the value that began with `=`: spreadsheets will display it as text, not execute it."
    )
    say()

    step(12, "Where things stand", "")
    s = c.get(f"{P}/ingestion/summary").json()
    block("summary", json.dumps(s, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
