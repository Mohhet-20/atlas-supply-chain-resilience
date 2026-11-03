"""Enterprise dashboard analytics API.

Read-only aggregation of records that other modules already persisted. Every metric is described by the registry (unit, calculation, time basis,
direction, sources, missing-data behaviour); unknown is reported as ``no_data`` or ``unavailable``, never as zero. Data is limited to the
caller's organisation; bodies and query strings naming an organisation are rejected. Exports need the editor role and are audited.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from app.analytics import registry as R
from app.analytics import service as S
from app.api.v1.deps import DbSession
from app.core.auth import Actor, Role, require
from app.core.errors import DomainValidationError
from app.registry import audit
from app.registry.audit import audit_scope, request_id_of
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/analytics", tags=["enterprise dashboard analytics"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 422)}
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Reviewer = Annotated[Actor, Depends(require(Role.REVIEWER))]
Lim = Annotated[int, Query(ge=1, le=100)]
Off = Annotated[int, Query(ge=0, le=100_000)]
FILTER_KEYS = {"window_days", "region", "category", "risk_class"}
EXPORT_AREAS = ("metrics", "supplier_risk", "inventory_at_risk", "single_source", "alerts", "activity")
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _strict(request: Request, *extra: str) -> None:
    """Reject unknown query parameters (this also rejects organisation / tenant parameters rather than silently ignoring them)."""
    bad = sorted(set(request.query_params) - FILTER_KEYS - set(extra))
    if bad:
        raise DomainValidationError(
            f"Unsupported parameter(s): {', '.join(bad)}. The organisation comes from the credential, never a parameter."
        )


def _filters(request: Request, window_days: int = R.DEFAULT_WINDOW, region: Annotated[str | None, Query(max_length=100)] = None, category: Annotated[str | None, Query(max_length=100)] = None,
             risk_class: Annotated[Literal["LOW", "MODERATE", "HIGH", "CRITICAL", "UNKNOWN"] | None, Query()] = None) -> S.Filters:  # fmt: skip
    if window_days not in R.WINDOWS:
        raise DomainValidationError(f"window_days must be one of {', '.join(map(str, R.WINDOWS))}.")
    return S.Filters(window_days, region or None, category or None, risk_class)


F = Annotated[S.Filters, Depends(_filters)]


def _cache(request: Request) -> S.TTLCache:
    c = getattr(request.app.state, "analytics_cache", None)
    if c is None:
        c = S.TTLCache(ttl=request.app.state.settings.analytics_cache_ttl_seconds)
        request.app.state.analytics_cache = c
    return c


def _cached(request: Request, actor: Actor, name: str, f: S.Filters, build, extra: tuple = ()) -> dict[str, Any]:  # noqa: ANN001
    cache = _cache(request)
    key = (name, actor.org_id, str(actor.role), f.key(), extra)  # never shared across organisations
    if cache.ttl > 0 and (hit := cache.get(key)) is not None:
        return {**hit, "meta": {**hit["meta"], "cached": True}}
    out = build()
    if cache.ttl > 0:
        cache.put(key, out)
    return out


def _findings(ms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Analytical findings (NOT operational alerts): fixed rules over the computed metrics, each citing its metric."""
    v = {m["id"]: m["value"] for m in ms if m["state"] == "ok"}
    rules = [
        (
            "RISK_HIGH_OR_CRITICAL",
            "suppliers are classified HIGH or CRITICAL by the configured risk rules",
            "/risk",
            "WARNING",
        ),
        (
            "RISK_UNASSESSED_SUPPLIERS",
            "active suppliers have unknown risk (no classified assessment); unknown is not low",
            "/risk",
            "INFO",
        ),
        (
            "INV_AT_RISK_PAIRS",
            "product-facility pairs are in shortage or projected stockout in the latest analysis",
            "/inventory",
            "WARNING",
        ),
        ("INV_UNKNOWN_PAIRS", "product-facility pairs could not be assessed (missing inputs)", "/inventory", "INFO"),
        (
            "NET_SINGLE_SOURCE_FACILITIES",
            "facilities depend structurally on a single confirmed supplier",
            "/network",
            "INFO",
        ),
        ("SCN_RUNS_FAILED", "scenario runs failed in the window", "/scenarios", "WARNING"),
        ("OPT_UNSUCCESSFUL_RUNS", "optimisation runs ended without a usable plan in the window", "/resilience", "INFO"),
        ("OPS_DECISIONS_UNDER_REVIEW", "decision cases are waiting for a reviewer", "/decision", "INFO"),
        ("RISK_REVIEW_PENDING", "risk assessments are waiting for review", "/risk", "INFO"),
    ]
    return [
        {"metric": mid, "severity": sev, "text": f"{int(v[mid])} {text}", "link": link, "kind": "finding"}
        for mid, text, link, sev in rules
        if mid in v and v[mid]
    ]


@router.get("/health", summary="Registry version, windows, limits and what is not available")
def health(request: Request, _a: Viewer) -> dict[str, Any]:
    st = request.app.state.settings
    return {"registry_version": R.REGISTRY_VERSION, "windows": list(R.WINDOWS), "default_window": R.DEFAULT_WINDOW, "areas": R.AREAS, "export_areas": list(EXPORT_AREAS), "cache_ttl_seconds": st.analytics_cache_ttl_seconds,
            "export_max_rows": st.analytics_export_max_rows, "unavailable": R.UNAVAILABLE, "timezone": "UTC", "scope": "Data is limited to the caller's organisation; within it, users see the data their role allows."}  # fmt: skip


@router.get("/metrics", summary="The metric registry")
def metrics(_a: Viewer) -> dict[str, Any]:
    return {"registry_version": R.REGISTRY_VERSION, "metrics": R.REGISTRY, "unavailable": R.UNAVAILABLE}


@router.get("/filters", summary="Values the filters can take (only dimensions the data supports)")
def filter_values(db: DbSession, _a: Viewer) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models.supplier import Supplier

    regions = [
        r
        for (r,) in db.execute(
            select(Supplier.country_or_region).distinct().order_by(Supplier.country_or_region).limit(200)
        )
    ]
    cats = [c for (c,) in db.execute(select(Supplier.category).distinct().order_by(Supplier.category).limit(200))]
    return {"windows": list(R.WINDOWS), "regions": regions, "categories": cats, "risk_classes": S.RISK_CLASSES + ["UNKNOWN"],
            "unsupported": ["organisation", "product", "facility", "lane", "scenario (these views link to their own workspaces)"]}  # fmt: skip


@router.get(
    "/overview", responses=ERR, summary="Executive overview: all metrics, analytical findings and recent activity"
)
def overview(request: Request, db: DbSession, f: F, actor: Viewer) -> dict[str, Any]:
    _strict(request)

    def build() -> dict[str, Any]:
        now = S.utcnow()
        ms = S.all_metrics(db, f, now)
        return {"meta": S.meta(f, now), "metrics": ms, "findings": _findings(ms), "unavailable": R.UNAVAILABLE,
                "findings_note": "Findings are fixed rules over the metrics shown; they are not operational alerts. Persisted alerts are under /analytics/alerts."}  # fmt: skip

    return _cached(request, actor, "overview", f, build)


@router.get("/area/{area}", responses=ERR, summary="Metrics of one area")
def area(
    area: Literal["network", "supplier_risk", "inventory", "logistics", "scenarios", "optimization", "operations"],
    request: Request,
    db: DbSession,
    f: F,
    actor: Viewer,
) -> dict[str, Any]:
    _strict(request)

    def build() -> dict[str, Any]:
        now = S.utcnow()
        return {"meta": S.meta(f, now, area), "metrics": S.area_metrics(db, area, f, now)}

    return _cached(request, actor, f"area:{area}", f, build)


@router.get(
    "/suppliers",
    responses=ERR,
    summary="Supplier-risk workspace: distribution, breakdowns, capacity exposure and a paginated ranked table",
)
def suppliers(request: Request, db: DbSession, f: F, actor: Viewer, limit: Lim = 20, offset: Off = 0) -> dict[str, Any]:
    _strict(request, "limit", "offset")
    now = S.utcnow()
    return {
        "meta": S.meta(f, now, "supplier_risk"),
        "metrics": S.supplier_risk(db, f, now),
        "breakdowns": S.supplier_breakdowns(db, f),
        "table": S.risk_table(db, f, limit, offset),
        "trend": S.risk_trend(db, f, now),
    }


@router.get(
    "/inventory", responses=ERR, summary="Inventory and fulfilment workspace (latest baseline stockout analysis)"
)
def inventory(
    request: Request,
    db: DbSession,
    f: F,
    actor: Viewer,
    limit: Lim = 20,
    offset: Off = 0,
    status: Annotated[
        Literal[
            "current_shortage", "projected_stockout", "low_coverage", "within_policy", "unknown", "insufficient_data"
        ]
        | None,
        Query(),
    ] = None,
) -> dict[str, Any]:
    _strict(request, "limit", "offset", "status")
    now = S.utcnow()
    return {
        "meta": S.meta(f, now, "inventory"),
        "metrics": S.inventory(db, f, now),
        "table": S.inventory_table(db, limit, offset, status),
    }


@router.get("/logistics", responses=ERR, summary="Logistics and delivery workspace")
def logistics(request: Request, db: DbSession, f: F, actor: Viewer) -> dict[str, Any]:
    _strict(request)
    now = S.utcnow()
    return {"meta": S.meta(f, now, "logistics"), "metrics": S.logistics(db, f, now), "lead_time_distribution": S.lead_time_histogram(db),
            "unavailable": [u for u in R.UNAVAILABLE if u["id"] in ("ON_TIME_DELIVERY_RATE", "TRANSPORT_COST", "LANE_UTILISATION")]}  # fmt: skip


@router.get("/network", responses=ERR, summary="Network dependency workspace (structural; use /graph for exploration)")
def network(request: Request, db: DbSession, f: F, actor: Viewer, limit: Lim = 20, offset: Off = 0) -> dict[str, Any]:
    _strict(request, "limit", "offset")
    now = S.utcnow()
    return {"meta": S.meta(f, now, "network"), "metrics": S.network(db, f, now), "criticality": S.criticality_distribution(db), "single_source": S.single_source(db, limit, offset),
            "note": "Adjacency is a structural dependency. It is not a quantified impact and does not imply cause."}  # fmt: skip


@router.get("/disruption", responses=ERR, summary="Scenario and optimisation workspace")
def disruption(
    request: Request, db: DbSession, f: F, actor: Viewer, limit: Lim = 10, offset: Off = 0
) -> dict[str, Any]:
    _strict(request, "limit", "offset")
    now = S.utcnow()
    return {"meta": S.meta(f, now, "scenarios"), "scenarios": S.scenarios(db, f, now), "optimization": S.optimization(db, f, now), "operations": S.operations(db, f, now), "runs": S.recent_runs(db, limit, offset)[offset : offset + limit],
            "note": "Scenario runs are hypothetical what-if analyses, not active disruptions. A run's reach counts structurally affected entities; it is not a loss. Optimisation results come from approximate searches unless the run says otherwise."}  # fmt: skip


@router.get("/trends", responses=ERR, summary="Time series: supplier-risk state and analysis activity")
def trends(request: Request, db: DbSession, f: F, actor: Viewer) -> dict[str, Any]:
    _strict(request)
    now = S.utcnow()
    return {
        "meta": S.meta(f, now, "supplier_risk"),
        "supplier_risk": S.risk_trend(db, f, now),
        "activity": S.activity_trend(db, now, f.window_days),
    }


@router.get("/alerts", responses=ERR, summary="Persisted alerts, priority-ordered (severity, then newest)")
def alerts(request: Request, db: DbSession, f: F, actor: Viewer, limit: Lim = 20, offset: Off = 0) -> dict[str, Any]:
    _strict(request, "limit", "offset")
    return {"meta": S.meta(f, S.utcnow(), "operations"), **S.alerts(db, f, limit, offset)}


@router.get("/activity", responses=ERR, summary="Recent runs, decisions and (reviewers and above) audit events")
def activity(request: Request, db: DbSession, actor: Viewer, limit: Lim = 20, offset: Off = 0) -> dict[str, Any]:
    _strict(request, "limit", "offset")
    return S.activity(db, limit, offset, include_audit=actor.role >= Role.REVIEWER, audit_where=audit_scope(actor))


def _safe(v: Any) -> Any:
    if isinstance(v, str) and v.startswith(FORMULA_START):
        return "'" + v
    return v


@router.get("/export", responses=ERR, summary="Export one analytics table as CSV or JSON (editors and above; audited)")
def export(request: Request, db: DbSession, f: F, actor: Editor, area: Literal["metrics", "supplier_risk", "inventory_at_risk", "single_source", "alerts", "activity"] = "metrics", fmt: Annotated[Literal["csv", "json"], Query(alias="format")] = "csv") -> Response:  # fmt: skip
    _strict(request, "area", "format")
    cap = request.app.state.settings.analytics_export_max_rows
    now = S.utcnow()
    if area == "metrics":
        rows = [{"metric_id": m["id"], "label": m["label"], "area": m["area"], "value": m["value"], "unit": m["unit"], "state": m["state"], "as_of": m["as_of"], "reason": m["reason"],
                 "calculation": R.BY_ID[m["id"]]["calculation"], "time_basis": R.BY_ID[m["id"]]["time_basis"]} for m in S.all_metrics(db, f, now)]  # fmt: skip
    elif area == "supplier_risk":
        rows = S.risk_table(db, f, cap, 0)["items"]
    elif area == "inventory_at_risk":
        rows = S.inventory_table(db, min(cap, 5000), 0, None)["items"]
    elif area == "single_source":
        rows = S.single_source(db, min(cap, 5000), 0)["items"]
    elif area == "alerts":
        rows = S.alerts(db, f, min(cap, 100), 0)["items"]
    else:
        rows = S.activity(
            db, min(cap, 100), 0, include_audit=actor.role >= Role.REVIEWER, audit_where=audit_scope(actor)
        )["items"]
    rows = rows[:cap]
    audit.record(
        db,
        actor,
        "analytics.exported",
        target_type="ANALYTICS_EXPORT",
        target_id=area,
        details={"format": fmt, "rows": len(rows), "filters": f.applied()},
        request_id=request_id_of(request),
    )
    db.commit()
    meta = {
        **S.meta(f, now),
        "area": area,
        "rows": len(rows),
        "row_cap": cap,
        "note": "Values are as stored; missing values are empty, never zero.",
    }
    if fmt == "json":
        return Response(
            json.dumps({"meta": meta, "rows": rows}, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="atlas-{area}.json"'},
        )
    buf = io.StringIO()
    cols = list(rows[0].keys()) if rows else ["empty"]
    w = csv.writer(buf)
    w.writerow(
        [
            f"# ATLAS analytics export {area}; generated {meta['generated_at']} UTC; filters {json.dumps(meta['applied_filters'])}; registry {R.REGISTRY_VERSION}"
        ]
    )
    w.writerow(cols)
    for r in rows:
        w.writerow(
            [
                ""
                if r[c] is None
                else _safe(r[c] if not isinstance(r[c], (dict, list)) else json.dumps(r[c], default=str))
                for c in cols
            ]
        )
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="atlas-{area}.csv"'},
    )
