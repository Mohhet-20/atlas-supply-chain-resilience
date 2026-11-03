# Sample ingestion files

**All records are fictional synthetic samples** (names start with "Synthetic"). They do not describe real
companies. They are safe to import into a local ATLAS database; they are stored with the provenance of the
source you register (normally `IMPORTED_EXTERNAL`), never as `SYNTHETIC_DEMO`.

| File | Entity | Demonstrates |
|---|---|---|
| `suppliers_valid.csv` | SUPPLIER | Canonical layout; blank `status` = unknown (default on create) |
| `suppliers_with_errors.csv` | SUPPLIER | DQ001, DQ002, DQ005, DQ010, DQ013; atomic rejection vs partial commit; formula-looking id |
| `suppliers_erp_export.csv` + `mapping_erp_suppliers.json` | SUPPLIER | Semicolon delimiter, foreign column names, value map (`J`/`N`) |
| `facilities.csv` | FACILITY | Owner reference, coordinates only where known, `0` capacity vs blank (unknown) |
| `facilities_units.csv` + `mapping_facility_units.json` | FACILITY | Unit conversion (units/day -> units/week) and an unsupported unit (pallets) |
| `products.csv` | PRODUCT | |
| `relationships.csv` | RELATIONSHIP | Reference resolution (`SMP-S-001` -> supplier in the same namespace) |
| `product_links.csv` | PRODUCT_LINK | Supplies / produces / stores; explicit `0` vs unknown quantity |
| `suppliers.json` | SUPPLIER | JSON `{"records": [...]}` input |

Import order matters because references are resolved against the registry: suppliers, facilities, products,
then relationships and product links. The executed walkthrough is `README.md`
(regenerate with `python scripts/ingest_walkthrough.py`).
