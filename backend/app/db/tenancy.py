"""Query-boundary tenant isolation.

Every SELECT/UPDATE/DELETE that touches a tenant-owned model gets ``organization_id = <session org>`` added automatically
(``with_loader_criteria``), including joins, lazy loads and ``Session.get``. A query cannot forget the filter, because the
filter is not in the query. The organisation comes from the *authenticated principal*, never from request data: either set
explicitly with ``scope_session`` or, for the per-request session from ``get_db``, read from the actor that authentication
recorded on the request. Inserts are stamped with the session organisation; an insert that names a different organisation,
or a row that references another organisation's row, is refused.

Sessions with no organisation (migrations, the CLI, platform tooling, unit tests) are unfiltered. A request session queried
before authentication has run is an error, not an unfiltered read. Code that needs to look across organisations on purpose
(the cross-tenant probe detector, key lookup) must say so with ``execution_options(skip_tenant=True)``.

Scope: every model deriving from ``TenantOwned`` (all domain data) plus the alerting/integration models in
``tenant_models()``. Child tables of tenant-owned alerting rows are reached only through their parent.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import event, select
from sqlalchemy.orm import Session, object_mapper, sessionmaker, with_loader_criteria
from sqlalchemy.orm.session import ORMExecuteState

from app.db.base import TenantOwned

_UNSET = object()


def tenant_models() -> tuple[type, ...]:
    from app.models.alerting import Alert, AlertEscalationPolicy, AlertEvent, AlertRule, AlertSuppression
    from app.models.integrations import (
        ApiKey,
        ExportJob,
        InboundEvent,
        Integration,
        IntegrationRun,
        ServiceAccount,
        SyncCheckpoint,
        SyncError,
        WebhookDelivery,
        WebhookEndpoint,
    )  # fmt: skip

    return (
        AlertEvent, AlertEscalationPolicy, AlertRule, Alert, AlertSuppression,
        ServiceAccount, ApiKey, Integration, IntegrationRun, SyncCheckpoint, SyncError, WebhookEndpoint, WebhookDelivery, InboundEvent, ExportJob,
    )  # fmt: skip


def scope_session(session: Session, org_id: int | None) -> None:
    session.info["org_id"] = org_id


def bind_request(session: Session, request_state) -> None:
    """Tie a per-request session to its request: the organisation is taken from the actor that authentication stores on
    ``request.state.actor``. Resolved at query time, so the order in which FastAPI resolves dependencies does not matter."""
    session.info["request_state"] = request_state


def session_org(session: Session) -> int | None:
    org = session.info.get("org_id", _UNSET)
    if org is not _UNSET:
        return org
    state = session.info.get("request_state")
    if state is None:
        return None
    actor = getattr(state, "actor", None)
    if actor is None:
        raise RuntimeError(
            "A request session was queried before the caller was authenticated; tenant scope is unknown."
        )
    return actor.org_id


def tenant_session_factory(factory: sessionmaker, org_id: int) -> sessionmaker:
    """A session factory whose sessions are scoped to one organisation, for work that outlives the request (background
    runs, hooks). Handing such work the plain factory would let it read and write across organisations. Such work writes
    its results, so its sessions are writer sessions (``app.db.session.immediate_bind``)."""
    from app.db.session import immediate_bind

    return sessionmaker(**(factory.kw | {"bind": immediate_bind(factory.kw["bind"]), "info": {"org_id": org_id}}))


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_filter(state: ORMExecuteState) -> None:
    if state.execution_options.get("skip_tenant"):
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    org = session_org(state.session)
    if org is None:
        return
    stmt = state.statement.options(
        with_loader_criteria(TenantOwned, lambda cls: cls.organization_id == org, include_aliases=True)
    )
    for model in tenant_models():
        stmt = stmt.options(with_loader_criteria(model, model.organization_id == org, include_aliases=True))
    state.statement = stmt


@event.listens_for(Session, "before_flush")
def _stamp_and_check(session: Session, _ctx, _instances) -> None:
    org = session_org(session)
    models = (*tenant_models(), TenantOwned)
    for obj in session.new:
        if isinstance(obj, models):
            if obj.organization_id is None:
                obj.organization_id = org if org is not None else 1
            elif org is not None and obj.organization_id != org:
                raise PermissionError("Refusing to write a record for another organisation.")
    if org is not None:
        _check_references(session, org)


def _check_references(session: Session, org: int) -> None:
    """Refuse new or changed domain rows that point at another organisation's row through a foreign key. Handlers look
    referenced records up through the filtered session first, so this only fires on a bug; it is the backstop that stops
    a numeric id belonging to another organisation from being linked in."""
    wanted: dict = defaultdict(set)
    for obj in (*session.new, *session.dirty):
        if not isinstance(obj, TenantOwned):
            continue
        mapper = object_mapper(obj)
        for column in mapper.local_table.columns:
            for fk in column.foreign_keys:
                target = fk.column.table
                if target.name == "organizations" or "organization_id" not in target.c:
                    continue
                value = getattr(obj, mapper.get_property_by_column(column).key, None)
                if value is not None:
                    wanted[(target, fk.column.name)].add(value)
    if not wanted:
        return
    with session.no_autoflush:
        for (table, key), values in wanted.items():
            ids = list(values)
            for i in range(0, len(ids), 500):
                foreign = session.execute(
                    select(table.c[key]).where(table.c[key].in_(ids[i : i + 500]), table.c.organization_id != org),
                    execution_options={"skip_tenant": True},
                ).first()
                if foreign is not None:
                    raise PermissionError("Refusing to link a record that belongs to another organisation.")


def note_probe(db: Session, model: type, column, value: str, kind: str) -> None:
    """Called when a lookup by identifier found nothing in the caller's organisation. If the identifier exists in *another*
    organisation this is a cross-tenant access attempt: remember it so the request layer can audit it. The caller still raises
    the same not-found error as for a missing record, so existence is never revealed."""
    if session_org(db) is None:
        return
    if db.scalar(select(column).where(column == value).execution_options(skip_tenant=True)) is not None:
        db.info.setdefault("probes", []).append({"kind": kind, "id": value})


def request_session_factory(request) -> sessionmaker:
    """The session factory for work started by a request but run outside its session, scoped to the caller's organisation."""
    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise RuntimeError("request_session_factory needs an authenticated request.")
    return tenant_session_factory(request.app.state.session_factory, actor.org_id)
