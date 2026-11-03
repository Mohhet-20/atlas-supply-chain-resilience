# Changelog

## 0.1.0 (unreleased)

First version.

- **Data:** supplier, facility and product registry; CSV/JSON ingestion with validation and lineage; duplicate
  detection and merges.
- **Network and quality:** supply network graph with integrity checks; data-quality rules, findings and scores.
- **Risk and disruption:** rule-based supplier risk; disruption scenarios; cascading-impact analysis.
- **Inventory and models:** inventory position and stockout projection; demand forecasting; lead-time and
  late-delivery models with a model registry.
- **Optimisation:** routing, sourcing allocation, inventory policies and multi-objective resilience optimisation;
  decision cases.
- **Monitoring:** dashboards with a documented metric registry; alert rules, alerts and escalation.
- **Platform:** organisations, users, sessions, API keys, RBAC and a hash-chained audit log; integrations with signed
  webhooks and exports.
- **Databases:** SQLite and PostgreSQL 16, with Alembic migrations 0001-0023.
- **Tooling:** synthetic demo world (`scripts/demo_world.py`); backend and frontend unit tests, Playwright browser and
  accessibility tests, and a load-test script.
