# ATLAS data directory

- `demo/demo_seed.json` (v2: 7 suppliers, 5 facilities, 11 relationships, 4 products, 8 product links, 12 observations) - the **only** demo dataset. Every record is fictional and carries
  `provenance: SYNTHETIC_DEMO`; identifiers are `DEMO-SUP-###`, `DEMO-FAC-###`, `DEMO-REL-###`, `DEMO-OBS-###`.
  Names (e.g. "Synthetic Supplier Alpha") are deliberately not real companies. Values are illustrative only.
- Runtime state (SQLite files, uploads, logs) is **not** stored here; it lives under `backend/var/` (git-ignored).

Load the demo data with `python scripts/seed_demo.py` after running `alembic upgrade head`
(see the root README). Seeding is idempotent, never overwrites or deletes existing records, and
aborts (rolling back everything) if a `DEMO-*` code collides with a non-demo record.
- `samples/` - small fictional CSV/JSON files and mapping profiles for trying the ingestion pipeline (namespace `sample-erp`). See `samples/README.md` and `README.md`. They are never loaded automatically.
