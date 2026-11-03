"""Network graph API: relationships, bounded traversal, path search, statistics and integrity diagnostics."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request

from app.api.v1.deps import DbSession
from app.core.auth import Actor, Role, require
from app.core.errors import DomainValidationError, NotFoundError, WriteDisabledError
from app.graph import integrity, service, stats
from app.graph.rules import LIMITS, NOT_MODELLED, RULES, EdgeKind, GraphRelType
from app.graph.store import EdgeFilter, NodeKey, fetch_incident, get_rec, list_edges, load_nodes, parse_ref
from app.graph.traversal import Expansion, expand, find_paths
from app.models.enums import EdgeStatus, RegistryType
from app.models.types import utcnow
from app.quality import hooks as quality_hooks
from app.registry.audit import request_id_of
from app.risk import hooks as risk_hooks
from app.schemas.common import ErrorResponse
from app.schemas.graph import (
    EdgeOut,
    GraphOut,
    PathOut,
    PathsOut,
    RelationshipCreate,
    RelationshipUpdate,
    StatusChange,
)

router = APIRouter(prefix="/graph", tags=["network graph"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
IdPath = Annotated[int, Path(ge=1, le=2**31 - 1)]
QId = Annotated[int, Query(ge=1, le=2**31 - 1)]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Reviewer = Annotated[Actor, Depends(require(Role.REVIEWER))]
Direction = Literal["upstream", "downstream", "both"]

Types = Annotated[list[GraphRelType] | None, Query(description="Relationship types (default: all).", max_length=6)]
Statuses = Annotated[
    list[EdgeStatus] | None, Query(description="Relationship statuses (default: CONFIRMED only).", max_length=4)
]
NodeTypes = Annotated[
    list[RegistryType] | None, Query(description="Only traverse into these node types.", max_length=3)
]
AsOf = Annotated[date | None, Query(description="Evaluate effective dates on this day (default: today).")]
IgnoreDates = Annotated[bool, Query(description="Do not filter by effective dates.")]
Depth = Annotated[int, Query(ge=1, le=LIMITS.max_depth, description=f"Hops to expand (max {LIMITS.max_depth}).")]
MaxNodes = Annotated[int, Query(ge=1, le=LIMITS.max_nodes, description=f"Node cap (max {LIMITS.max_nodes}).")]
MaxEdges = Annotated[int, Query(ge=1, le=LIMITS.max_edges, description=f"Edge cap (max {LIMITS.max_edges}).")]


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError("Graph write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED).")


def _day(as_of: date | None) -> date:
    return as_of or utcnow().date()


def _flt(
    types: Types = None, statuses: Statuses = None, node_types: NodeTypes = None, as_of: AsOf = None,
    ignore_dates: IgnoreDates = False,
) -> EdgeFilter:  # fmt: skip
    return EdgeFilter(
        types=frozenset(types) if types else None,
        statuses=frozenset(statuses) if statuses else frozenset({EdgeStatus.CONFIRMED}),
        node_types=frozenset(node_types) if node_types else None,
        as_of=None if ignore_dates else _day(as_of),
    )


Flt = Annotated[EdgeFilter, Depends(_flt)]


def _limits_dict(depth: int | None = None, nodes: int | None = None, edges: int | None = None) -> dict[str, Any]:
    return {
        "max_depth": LIMITS.max_depth, "max_nodes": LIMITS.max_nodes, "max_edges": LIMITS.max_edges,
        "applied_depth": depth, "applied_max_nodes": nodes, "applied_max_edges": edges,
    }  # fmt: skip


def _graph_out(
    db: DbSession, exp: Expansion, start_node, redirected, nodes_cap: int | None, edges_cap: int | None
) -> GraphOut:
    keys = set(exp.depth_of)
    node_recs = load_nodes(db, keys)
    nodes = [
        service.node_out(node_recs[k], exp.depth_of[k])
        for k in sorted(keys, key=lambda k: (exp.depth_of[k], k[0].value, k[1]))
        if k in node_recs
    ]
    edges = [service.edge_out(e, exp.hop_of.get(e.ref)) for e in sorted(exp.edges.values(), key=lambda e: e.ref)]
    return GraphOut(
        start=service.node_out(start_node, 0), direction=exp.direction, nodes=nodes, edges=edges,
        nodes_returned=len(nodes), edges_returned=len(edges), requested_depth=exp.requested_depth,
        max_depth_reached=exp.max_depth_reached, nodes_at_depth_limit=exp.nodes_at_depth_limit,
        truncated=exp.truncated, truncation_reasons=exp.truncation_reasons,
        limits=_limits_dict(exp.requested_depth, nodes_cap, edges_cap), resolved_from=redirected,
    )  # fmt: skip


# ---------------------------------------------------------------------------------------------------- overview


@router.get("/config", responses=ERR, summary="Relationship vocabulary, direction semantics and hard limits")
def config(_a: Viewer) -> dict[str, Any]:
    return {
        "relationship_types": [
            {
                "type": r.type.value,
                "source": r.source.value,
                "destination": r.destination.value,
                "editable": r.editable,
                "cycles": r.cycles,
                "meaning": r.meaning,
            }
            for r in RULES.values()
        ],  # fmt: skip
        "not_modelled": NOT_MODELLED,
        "statuses": [s.value for s in EdgeStatus],
        "default_statuses": [EdgeStatus.CONFIRMED.value],
        "direction": "Edges point along material/commercial flow. Downstream follows source -> destination; upstream the reverse.",
        "limits": LIMITS.__dict__ | {},
    }


@router.get("/summary", responses=ERR, summary="Network statistics (counts, isolated nodes, components)")
def summary(db: DbSession, _a: Viewer, as_of: AsOf = None) -> dict[str, Any]:
    with db.begin():
        return stats.summary(db, _day(as_of))


# ---------------------------------------------------------------------------------------------- relationships


@router.get("/relationships", responses=ERR, summary="List relationships (paginated, filterable)")
def list_relationships(
    db: DbSession, _a: Viewer, flt: Flt,
    node_type: Annotated[RegistryType | None, Query()] = None,
    node_id: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> dict[str, Any]:  # fmt: skip
    if (node_type is None) != (node_id is None):
        raise DomainValidationError("node_type and node_id must be given together.")
    with db.begin():
        node: NodeKey | None = None
        if node_type is not None and node_id is not None:
            node = service.resolve_node(db, node_type, node_id)[0]
        items, total = list_edges(db, flt, node, limit, offset)
        return {
            "items": [service.edge_out(e) for e in items], "total": total, "limit": limit, "offset": offset,
            "filters": {"statuses": sorted(s.value for s in flt.statuses), "as_of": flt.as_of},
        }  # fmt: skip


@router.get("/relationships/{ref}", responses=ERR, summary="One relationship with endpoints and history")
def get_relationship(ref: str, db: DbSession, _a: Viewer) -> dict[str, Any]:
    kind, row_id = parse_ref(ref)
    with db.begin():
        rec = get_rec(db, kind, row_id)
        if rec is None:
            raise NotFoundError(f"Relationship {ref} was not found.")
        nodes = load_nodes(db, [rec.source, rec.destination])
        return {
            "relationship": service.edge_out(rec),
            "source": service.node_out(nodes[rec.source]) if rec.source in nodes else None,
            "destination": service.node_out(nodes[rec.destination]) if rec.destination in nodes else None,
            "history": service.history(db, kind, row_id),
            "allowed_transitions": sorted(s.value for s in service.TRANSITIONS[rec.status])
            if rec.kind is not EdgeKind.OWNER
            else [],
        }  # fmt: skip


@router.post("/relationships", status_code=201, response_model=EdgeOut, responses=ERR, summary="Create a relationship")
def create_relationship(body: RelationshipCreate, db: DbSession, request: Request, actor: Editor) -> EdgeOut:
    _writes(request)
    with db.begin():
        rec = service.create_relationship(db, actor, body, request_id_of(request))
        out = service.edge_out(rec)
        ref = rec.ref
    quality_hooks.reevaluate_edge(request, ref, actor.name)
    risk_hooks.reassess_edge(request, ref, actor.name)
    return out


@router.patch("/relationships/{ref}", response_model=EdgeOut, responses=ERR, summary="Update relationship attributes")
def update_relationship(ref: str, body: RelationshipUpdate, db: DbSession, request: Request, actor: Editor) -> EdgeOut:
    _writes(request)
    with db.begin():
        out = service.edge_out(service.update_relationship(db, actor, ref, body, request_id_of(request)))
    quality_hooks.reevaluate_edge(request, ref, actor.name)
    risk_hooks.reassess_edge(request, ref, actor.name)
    return out


@router.post("/relationships/{ref}/status", response_model=EdgeOut, responses=ERR, summary="Change relationship status")
def change_status(ref: str, body: StatusChange, db: DbSession, request: Request, actor: Reviewer) -> EdgeOut:
    _writes(request)
    with db.begin():
        out = service.edge_out(service.change_status(db, actor, ref, body, request_id_of(request)))
    quality_hooks.reevaluate_edge(request, ref, actor.name)
    risk_hooks.reassess_edge(request, ref, actor.name)
    return out


# ------------------------------------------------------------------------------------------------- traversal


@router.get(
    "/nodes/{node_type}/{node_id}/neighbors", response_model=GraphOut, responses=ERR, summary="Direct neighbours"
)
def neighbors(
    node_type: RegistryType, node_id: IdPath, db: DbSession, _a: Viewer, flt: Flt,
    direction: Direction = "both", max_edges: MaxEdges = LIMITS.default_edges,
) -> GraphOut:  # fmt: skip
    with db.begin():
        key, node, redirected = service.resolve_node(db, node_type, node_id)
        exp = expand(db, key, direction, 1, flt, LIMITS.max_nodes, max_edges)
        out = _graph_out(db, exp, node, redirected, LIMITS.max_nodes, max_edges)
        up = sum(1 for e in exp.edges.values() if e.destination == key)
        down = sum(1 for e in exp.edges.values() if e.source == key)
        out.limits = out.limits | {
            "upstream_count": up, "downstream_count": down, "counts_exact": not exp.truncated,
        }  # fmt: skip
        return out


def _directional(
    direction: Direction, node_type: RegistryType, node_id: int, db: DbSession, flt: EdgeFilter,
    depth: int, max_nodes: int, max_edges: int,
) -> GraphOut:  # fmt: skip
    with db.begin():
        key, node, redirected = service.resolve_node(db, node_type, node_id)
        exp = expand(db, key, direction, depth, flt, max_nodes, max_edges)
        return _graph_out(db, exp, node, redirected, max_nodes, max_edges)


@router.get(
    "/nodes/{node_type}/{node_id}/upstream", response_model=GraphOut, responses=ERR, summary="Upstream exploration"
)
def upstream(
    node_type: RegistryType, node_id: IdPath, db: DbSession, _a: Viewer, flt: Flt,
    depth: Depth = LIMITS.default_depth, max_nodes: MaxNodes = LIMITS.default_nodes,
    max_edges: MaxEdges = LIMITS.default_edges,
) -> GraphOut:  # fmt: skip
    return _directional("upstream", node_type, node_id, db, flt, depth, max_nodes, max_edges)


@router.get(
    "/nodes/{node_type}/{node_id}/downstream", response_model=GraphOut, responses=ERR, summary="Downstream exploration"
)
def downstream(
    node_type: RegistryType, node_id: IdPath, db: DbSession, _a: Viewer, flt: Flt,
    depth: Depth = LIMITS.default_depth, max_nodes: MaxNodes = LIMITS.default_nodes,
    max_edges: MaxEdges = LIMITS.default_edges,
) -> GraphOut:  # fmt: skip
    return _directional("downstream", node_type, node_id, db, flt, depth, max_nodes, max_edges)


@router.get("/expand", response_model=GraphOut, responses=ERR, summary="Bounded graph expansion from a start node")
def expand_graph(
    start_type: RegistryType, start_id: QId, db: DbSession, _a: Viewer, flt: Flt,
    direction: Direction = "both", depth: Depth = LIMITS.default_depth, max_nodes: MaxNodes = LIMITS.default_nodes,
    max_edges: MaxEdges = LIMITS.default_edges,
) -> GraphOut:  # fmt: skip
    return _directional(direction, start_type, start_id, db, flt, depth, max_nodes, max_edges)


@router.get("/paths", response_model=PathsOut, responses=ERR, summary="Bounded simple-path search between two records")
def paths(
    from_type: RegistryType, from_id: QId, to_type: RegistryType, to_id: QId, db: DbSession, _a: Viewer,
    flt: Flt,
    mode: Annotated[Literal["directed", "undirected"], Query(description="directed follows flow from -> to")] = "directed",
    max_length: Annotated[int, Query(ge=1, le=LIMITS.max_path_length)] = LIMITS.default_path_length,
    max_paths: Annotated[int, Query(ge=1, le=LIMITS.max_paths)] = LIMITS.default_paths,
) -> PathsOut:  # fmt: skip
    with db.begin():
        a_key, a_node, a_red = service.resolve_node(db, from_type, from_id)
        b_key, b_node, b_red = service.resolve_node(db, to_type, to_id)
        result = find_paths(db, a_key, b_key, flt, mode=mode, max_length=max_length, max_paths=max_paths)
        out: list[PathOut] = []
        node_cache = load_nodes(
            db, {k for p in result.paths for e in p for k in (e.source, e.destination)} | {a_key, b_key}
        )
        for edges in result.paths:
            seq = [a_key]
            for e in edges:
                seq.append(e.destination if e.source == seq[-1] else e.source)
            out.append(
                PathOut(
                    length=len(edges),
                    nodes=[service.node_out(node_cache[k], i) for i, k in enumerate(seq)],
                    edges=[service.edge_out(e, i + 1) for i, e in enumerate(edges)],
                    relationship_types=[e.type for e in edges],
                    provenance=sorted({e.provenance for e in edges}),
                    statuses=sorted({e.status for e in edges}, key=lambda s: s.value),
                )  # fmt: skip
            )
        found = bool(out) or a_key == b_key
        note = (
            "Same record." if a_key == b_key
            else "Only stored relationships matching the filters were followed; no path is inferred."
            if found else "No path was found within the limits using the selected filters. This does not prove the "
            "records are unconnected: proposed, inactive or out-of-period relationships are excluded by default."
        )  # fmt: skip
        return PathsOut(
            start=service.node_out(a_node, 0), goal=service.node_out(b_node), mode=mode, paths=out, found=found,
            shortest_length=0 if a_key == b_key else result.shortest_length, truncated=result.truncated,
            truncation_reasons=result.reasons, note=note, resolved_from={"from": a_red, "to": b_red} if a_red or b_red else None,
        )  # fmt: skip


# --------------------------------------------------------------------------------------------------- integrity


@router.get("/integrity", responses=ERR, summary="Graph integrity diagnostics (read-only)")
def graph_integrity(db: DbSession, _a: Reviewer, as_of: AsOf = None) -> dict[str, Any]:
    with db.begin():
        return integrity.run(db, _day(as_of))


_ = fetch_incident
