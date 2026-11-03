# Tenant isolation

## Scope

Every organisation's data is separated. Before 2026-11-03 only alerting, audit, users and integrations were;
migration 0022 added the remaining 80 domain tables:
- **Network:** suppliers, facilities, products, relationships, sources, identifiers, aliases, ingestion batches and
  lineage.
- **Data quality and risk:** quality runs and findings; risk runs, assessments, evidence and reviews.
- **Analysis:** scenarios and simulations, impact analyses, inventory and stockout analyses, forecasts, lead-time
  models, the model registry.
- **Optimisation and decisions:** routing, sourcing, replenishment and resilience runs, and decision cases.

Users can still find out how many records exist in total, because record ids are global sequential integers; see
[Residual risks](#residual-risks).

## Mechanism (`backend/app/db/tenancy.py`)

- **Where the organisation comes from.** Authentication records the actor on `request.state.actor`. The request's
  database session is bound to the request (`bind_request`), and its organisation is read from that actor at query
  time. Work that outlives the request gets a session factory scoped to the caller's organisation
  (`request_session_factory`): background analysis runs, quality and risk hooks. The organisation never comes from
  request data.
- **Reads.** A global SQLAlchemy `do_orm_execute` listener adds `organization_id = :org` to every SELECT, UPDATE
  and DELETE on a tenant-owned model. That covers joins, relationship loads and `Session.get`. All domain models
  share one `TenantOwned` mixin, so one `with_loader_criteria` option covers all of them. A query cannot forget the
  filter, because the filter is not written in the query.
- **Writes.** A `before_flush` hook stamps `organization_id` on new rows and refuses a row that names another
  organisation.
- **References.** The same hook checks every foreign key of new and changed domain rows. A row pointing at another
  organisation's row is refused. Handlers already look references up through the filtered session, so this is the
  backstop.
- **Fail closed.** A request session queried before authentication has run raises an error instead of reading
  unfiltered. A test asserts that every route with a database session authenticates.
- **Uniqueness is per organisation.** That covers business codes, source namespaces, mapping-profile names, model keys,
  idempotency keys, import fingerprints and "one active run per scope" guards. A uniqueness conflict therefore can
  never reveal that another organisation holds a record. Quality-rule overrides are keyed by (organisation, rule).
- **Caches.** Dashboard, network and analytics response caches include the organisation in every key.
- **Probes.** Client-supplied `organization_id` and `tenant_id` fields are rejected (422). Another organisation's
  record answers 404, exactly like a missing one. For alerts and webhooks the probe is also audited
  (`security.cross_tenant_access`).
- **Deliberate cross-organisation code.** API-key lookup and the probe detector say so with
  `execution_options(skip_tenant=True)`; platform job counts use a session with no organisation. Such sessions, also used by the CLI, migrations and unit tests,
  are unfiltered.
- **Existing data.** Migration 0022 assigns every existing row to the default organisation (id 1), so an upgraded
  single-organisation deployment behaves exactly as before.

## Tested

- **`backend/tests/test_security_domain_tenancy.py`, 16 tests:**
  - Per-organisation lists, search and the network graph.
  - Cross-organisation reads and updates are 404 and change nothing.
  - Cross-organisation references are refused.
  - The same source namespace and the same CSV are imported independently by two organisations.
  - Quality-rule overrides are per organisation.
  - A background risk run stays inside its organisation, as do pull alert detectors.
  - Caches are never shared across organisations. This test fails without the cache fix: a real leak found while
    writing it.
  - The mechanism itself: filtering and stamping, reference refusal, scoped background factories, failing closed
    before authentication, and every database route authenticating.
- **`backend/tests/test_security_tenancy.py`, 25 tests:** the alerting, audit and integrations scope.
- **Migration 0022 on SQLite and PostgreSQL 16:**
  - Upgrade, downgrade and models-versus-schema drift (`tests/test_migrations.py`,
    `tests/test_migrations_any_database.py`).
  - A schema comparison before and after confirmed that no CHECK constraint, foreign key, index or append-only
    trigger was lost.

## Residual risks

- **Global sequential ids.** Record ids are global integers, so a user can estimate how many records exist across
  all organisations. Ids never reveal content: another organisation's id answers 404.
- **Raw SQL bypasses the filter.** Raw SQL, or a Core statement against `Model.__table__`, would not be filtered.
  The application has none outside the health check and migrations. New code must use ORM statements.
- **Shared database.** All organisations share one database and one encryption boundary. No per-tenant encryption
  or residency is offered.
- **No external review.** The isolation has not been reviewed outside this project.
