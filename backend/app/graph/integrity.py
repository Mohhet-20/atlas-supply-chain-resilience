"""Read-only graph integrity diagnostics. Nothing here modifies data; each finding carries a recommendation."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.graph.rules import LIMITS, RULES, EdgeKind, GraphRelType
from app.graph.store import EdgeRec, NodeKey, link_rec, owner_rec, supply_rec
from app.models.enums import EdgeStatus, RegistryType
from app.models.facility import Facility
from app.models.product import Product, ProductLink
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier

S, F, P = RegistryType.SUPPLIER, RegistryType.FACILITY, RegistryType.PRODUCT
MODEL = {S: Supplier, F: Facility, P: Product}
CHECKS = {
    "GRP001": ("ERROR", "Dangling entity reference"),
    "GRP002": ("ERROR", "Invalid relationship type / endpoint combination"),
    "GRP003": ("ERROR", "Duplicate relationship"),
    "GRP004": ("ERROR", "Self-link"),
    "GRP005": ("WARNING", "Cycle in a cycle-tolerant relationship type"),
    "GRP006": ("ERROR", "Relationship references a merged (tombstoned) record"),
    "GRP007": ("WARNING", "Active relationship on an inactive, on-hold or retired record"),
    "GRP008": ("ERROR", "Invalid effective date range"),
    "GRP009": ("WARNING", "Contradictory status or provenance"),
}


def _load_lifecycles(session: Session, keys: set[NodeKey]) -> dict[NodeKey, str]:
    out: dict[NodeKey, str] = {}
    for rtype, model in MODEL.items():
        ids = sorted(i for t, i in keys if t is rtype)
        for start in range(0, len(ids), LIMITS.in_chunk):
            cols = [model.id, model.merged_into_id, model.status]
            if rtype is P:
                cols.append(model.retired_at)
            for row in session.execute(select(*cols).where(model.id.in_(ids[start : start + LIMITS.in_chunk]))):
                merged, status = row[1], row[2]
                retired = row[3] if rtype is P else None
                out[(rtype, row[0])] = "MERGED" if merged else "RETIRED" if retired else status.value
    return out


def run(session: Session, day: date) -> dict[str, Any]:
    cap = LIMITS.scan_cap_integrity
    edges: list[EdgeRec] = []
    scan_truncated = False
    structure_problems: list[tuple[str, str, str]] = []  # (ref, code, message) found from raw columns
    for r in session.scalars(select(SupplyRelationship).order_by(SupplyRelationship.id).limit(cap + 1)).unique():
        if len(edges) >= cap:
            scan_truncated = True
            break
        try:
            edges.append(supply_rec(r))
        except Exception:  # pragma: no cover - unreadable endpoint columns
            structure_problems.append(
                (f"SUPPLY:{r.id}", "GRP002", "Endpoint columns do not form a valid relationship.")
            )
        else:
            if r.relationship_type.value == "SUPPLIES" and (r.supplier_id is None or r.source_facility_id is not None):
                structure_problems.append(
                    (f"SUPPLY:{r.id}", "GRP002", "SUPPLIES must have a supplier and no source facility.")
                )
            if r.relationship_type.value == "TRANSFERS_TO" and (
                r.source_facility_id is None or r.supplier_id is not None
            ):
                structure_problems.append(
                    (f"SUPPLY:{r.id}", "GRP002", "TRANSFERS_TO must have a source facility and no supplier.")
                )
    remaining = cap - len(edges)
    for link in session.scalars(select(ProductLink).order_by(ProductLink.id).limit(max(remaining, 0) + 1)).unique():
        if len(edges) >= cap:
            scan_truncated = True
            break
        edges.append(link_rec(link))
        if (link.supplier_id is None) == (link.facility_id is None):
            structure_problems.append(
                (f"PRODUCT:{link.id}", "GRP002", "A product link needs exactly one of supplier or facility.")
            )
    for f in session.scalars(
        select(Facility).where(Facility.owner_supplier_id.is_not(None)).order_by(Facility.id).limit(cap)
    ).unique():
        edges.append(owner_rec(f))

    keys: set[NodeKey] = set()
    for e in edges:
        keys.add(e.source)
        keys.add(e.destination)
    life = _load_lifecycles(session, {k for k in keys if k[1] is not None})

    issues: list[dict[str, Any]] = []

    def add(code: str, refs: list[str], nodes: list[NodeKey], message: str, fix: str) -> None:
        sev, title = CHECKS[code]
        issues.append(
            {"code": code, "severity": sev, "title": title, "message": message, "relationship_refs": refs,
             "nodes": [{"type": t.value, "id": i} for t, i in nodes], "recommendation": fix}
        )  # fmt: skip

    for ref, code, msg in structure_problems:
        add(
            code,
            [ref],
            [],
            msg,
            "Do not edit rows by hand. Reject or deactivate the relationship and recreate it with valid endpoints.",
        )

    identities: dict[tuple, list[str]] = defaultdict(list)
    for e in edges:
        rule = RULES[e.type]
        identities[(e.type, e.source, e.destination)].append(e.ref)
        for key in (e.source, e.destination):
            if key[1] is None or key not in life:
                add(
                    "GRP001",
                    [e.ref],
                    [key],
                    f"{e.ref} points at {key[0].value} {key[1]}, which does not exist.",
                    "Restore the registry record or reject the relationship; do not delete history.",
                )
            elif life[key] == "MERGED":
                add(
                    "GRP006",
                    [e.ref],
                    [key],
                    f"{e.ref} still references {key[0].value} {key[1]}, which was merged away.",
                    "Re-run or compensate the registry merge so relationships move to the survivor.",
                )
        if (e.source[0], e.destination[0]) != (rule.source, rule.destination):
            add(
                "GRP002",
                [e.ref],
                [e.source, e.destination],
                f"{e.type.value} cannot connect {e.source[0].value} to {e.destination[0].value}.",
                "Reject the relationship and create one of the correct type.",
            )
        if e.source == e.destination:
            add("GRP004", [e.ref], [e.source], f"{e.ref} connects a record to itself.", "Reject the relationship.")
        if e.effective_from and e.effective_to and e.effective_to < e.effective_from:
            add(
                "GRP008",
                [e.ref],
                [],
                f"{e.ref} ends ({e.effective_to}) before it starts ({e.effective_from}).",
                "Correct the effective dates through the update endpoint.",
            )
        active = e.status is EdgeStatus.CONFIRMED and e.effective_on(day)
        if active:
            for key in (e.source, e.destination):
                if life.get(key) in ("INACTIVE", "ON_HOLD", "RETIRED"):
                    add(
                        "GRP007",
                        [e.ref],
                        [key],
                        f"{e.ref} is active but {key[0].value} {key[1]} is {life[key]}.",
                        "Deactivate the relationship if the record is out of service, or reactivate the record.",
                    )
        if e.status is EdgeStatus.CONFIRMED and e.provenance in ("MODEL_ESTIMATE", "SIMULATED"):
            add(
                "GRP009",
                [e.ref],
                [],
                f"{e.ref} is CONFIRMED but its provenance is {e.provenance}, i.e. not an observed fact.",
                "Have a reviewer re-check the evidence, or return the relationship to PROPOSED.",
            )
        if e.status is EdgeStatus.CONFIRMED and e.effective_to and e.effective_to < day:
            add(
                "GRP009",
                [e.ref],
                [],
                f"{e.ref} is CONFIRMED but its effective period ended on {e.effective_to}.",
                "Deactivate the relationship or extend its effective dates with a reason.",
            )
        if e.status in (EdgeStatus.REJECTED, EdgeStatus.INACTIVE) and not e.status_reason:
            add(
                "GRP009",
                [e.ref],
                [],
                f"{e.ref} is {e.status.value} without a recorded reason.",
                "Add the reason in the next status change; history cannot be rewritten.",
            )
    for (typ, src, dst), refs in identities.items():
        if len(refs) > 1:
            add(
                "GRP003",
                refs,
                [src, dst],
                f"{len(refs)} relationships share the identity {typ.value} {src[0].value}:{src[1]} -> {dst[0].value}:{dst[1]}.",
                "Keep one, reject the others, and record why (the registry merge workflow resolves duplicates created by merges).",
            )

    # cycles only matter for cycle-tolerant types; others are bipartite by construction
    adj: dict[NodeKey, list[tuple[NodeKey, str]]] = defaultdict(list)
    for e in edges:
        if RULES[e.type].cycles == "allowed" and e.status is EdgeStatus.CONFIRMED and e.effective_on(day):
            adj[e.source].append((e.destination, e.ref))
    for component, refs in _cycles(adj)[:20]:
        add(
            "GRP005",
            refs,
            component,
            f"{len(component)} facilities form a transfer cycle.",
            "Cycles can be legitimate (returns, rebalancing). Confirm each link is intended; no data was changed.",
        )

    counts = Counter(i["code"] for i in issues)
    issues.sort(
        key=lambda i: ({"ERROR": 0, "WARNING": 1, "INFO": 2}[i["severity"]], i["code"], i["relationship_refs"][:1])
    )
    shown = issues[: LIMITS.integrity_issue_cap]
    return {
        "as_of": day,
        "scanned_relationships": len(edges),
        "scan_truncated": scan_truncated,
        "issue_count": len(issues),
        "by_code": dict(counts),
        "by_severity": dict(Counter(i["severity"] for i in issues)),
        "issues": shown,
        "issues_truncated": len(issues) > len(shown),
        "checks": [{"code": c, "severity": s, "title": t} for c, (s, t) in CHECKS.items()],
        "remediation": "Diagnostics are read-only. Fix findings with the relationship update/status endpoints or the "
        "registry merge workflow; nothing is deleted or rewritten automatically.",
        "acyclic_policy": {t.value: r.cycles for t, r in RULES.items()},
    }


def _cycles(adj: dict[NodeKey, list[tuple[NodeKey, str]]]) -> list[tuple[list[NodeKey], list[str]]]:
    """Strongly connected components with a cycle (iterative Tarjan)."""
    index: dict[NodeKey, int] = {}
    low: dict[NodeKey, int] = {}
    on: set[NodeKey] = set()
    stack: list[NodeKey] = []
    found: list[tuple[list[NodeKey], list[str]]] = []
    counter = 0
    for root in sorted(adj, key=lambda k: (k[0].value, k[1])):
        if root in index:
            continue
        work = [(root, iter(adj.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt, _ref in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(adj.get(nxt, ()))))
                    advanced = True
                    break
                if nxt in on:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1:
                    members = set(comp)
                    refs = sorted(r for n in comp for (m, r) in adj.get(n, ()) if m in members)
                    found.append((sorted(comp, key=lambda k: (k[0].value, k[1])), refs))
    return found


_ = (EdgeKind, GraphRelType)
