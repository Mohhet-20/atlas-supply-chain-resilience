"""Unified registry search across suppliers, facilities and products.

Strategy (bounded, no all-rows scans in Python):
  1. Exact internal id / code                 -> indexed equality
  2. Exact external identifier                -> indexed equality (entity columns + entity_identifiers)
  3. Exact normalised name / alias            -> indexed equality
  4. Prefix of the normalised name            -> indexed range (LIKE 'q%')
  5. Substring                                -> LIKE '%q%' (bounded by ``pool_limit``)
  6. Approximate (fuzzy)                      -> candidate pool fetched with token LIKE queries (bounded by
                                                 ``pool_limit``), then scored with a deterministic similarity
Every hit carries explicit ``match_reasons``; approximate hits are flagged and never treated as identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import false, or_, select
from sqlalchemy.orm import Session

from app.models.enums import EntityStatus, RegistryType
from app.models.registry import EntityAlias, EntityIdentifier
from app.models.source import DataSource
from app.registry.entities import CODE_ATTR, MODELS, Entity, canonical_of, code_of
from app.registry.normalize import core_name, like_escape, name_tokens, normalize_identifier, normalize_name

REASON_RANK = {
    "EXACT_ID": 0,
    "EXACT_CODE": 1,
    "EXACT_EXTERNAL_ID": 2,
    "EXACT_NAME": 3,
    "EXACT_ALIAS": 4,
    "PREFIX": 5,
    "CONTAINS": 6,
    "FUZZY": 7,
}
SORTS = ("relevance", "name", "updated")


@dataclass
class SearchParams:
    q: str
    types: list[RegistryType] = field(default_factory=lambda: list(RegistryType))
    status: EntityStatus | None = None
    country: str | None = None
    category: str | None = None
    include_merged: bool = False
    sort: str = "relevance"
    limit: int = 25
    offset: int = 0
    pool_limit: int = 200  # per type and per strategy
    fuzzy_enabled: bool = True
    fuzzy_threshold: float = 0.72


def similarity(a: str, b: str) -> float:
    """Deterministic similarity in [0, 1] between two names (legal forms removed, token-order insensitive)."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    jaccard = len(sa & sb) / len(sa | sb)
    ratio = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return round(max(jaccard, ratio), 4)


def _scope(model, p: SearchParams):
    conds = []
    if not p.include_merged:
        conds.append(model.merged_into_id.is_(None))
    if p.status is not None:
        conds.append(model.status == p.status)
    if p.country:
        if hasattr(model, "country_or_region"):
            conds.append(model.country_or_region.ilike(like_escape(p.country), escape="\\"))
        else:
            conds.append(false())  # this entity type has no country attribute, so it cannot match a country filter
    if p.category and hasattr(model, "category"):
        conds.append(model.category.ilike(like_escape(p.category), escape="\\"))
    return conds


def _canonical_hit(session, e, p):
    """Exact identifier/alias/code hits on a merged record resolve to the live canonical record, so old references keep working."""
    if e is None or e.merged_into_id is None or p.include_merged:
        return e, None
    c = canonical_of(session, e)
    if c is None or c.merged_into_id is not None:
        return None, None
    return c, code_of(e)


def _add(hits: dict, rtype: RegistryType, entity: Entity, reason: dict[str, Any]) -> None:
    key = (rtype, entity.id)
    hit = hits.setdefault(key, {"entity": entity, "type": rtype, "reasons": []})
    if not any(r["kind"] == reason["kind"] and r.get("detail") == reason.get("detail") for r in hit["reasons"]):
        hit["reasons"].append(reason)


def search(session: Session, p: SearchParams) -> dict[str, Any]:
    raw = p.q.strip()
    norm = normalize_name(raw)
    hits: dict[tuple[RegistryType, int], dict[str, Any]] = {}
    truncated = False
    ident = normalize_identifier(raw)
    ns_scoped: tuple[str, str] | None = None
    if ":" in ident:  # namespace:external_id syntax
        ns, _, val = ident.partition(":")
        if ns and val:
            ns_scoped = (ns, val)

    for rtype in p.types:
        model = MODELS[rtype]
        base = _scope(model, p)

        def run(extra, limit=p.pool_limit, base=base, model=model):
            return list(session.scalars(select(model).where(*base, *extra).order_by(model.id).limit(limit)))

        # 1. internal id / code
        if raw.isdigit() and len(raw) <= 10:
            for e in run([model.id == int(raw)]):
                _add(hits, rtype, e, {"kind": "EXACT_ID", "field": "id", "exact": True, "detail": raw})
        for e in run([getattr(model, CODE_ATTR[rtype]) == raw]):
            _add(hits, rtype, e, {"kind": "EXACT_CODE", "field": CODE_ATTR[rtype], "exact": True, "detail": raw})

        # 2. exact external identifiers (primary pair + additional identifiers, optionally namespace-scoped)
        prim = [model.external_id == ident]
        if ns_scoped:
            prim = [
                or_(
                    model.external_id == ident,
                    (model.external_id == ns_scoped[1])
                    & model.source_id.in_(select(DataSource.id).where(DataSource.namespace == ns_scoped[0])),
                )
            ]
        for e in run(prim):
            _add(hits, rtype, e, {"kind": "EXACT_EXTERNAL_ID", "field": "external_id", "exact": True, "detail": ident})
        id_stmt = select(EntityIdentifier).where(
            EntityIdentifier.entity_type == rtype,
            EntityIdentifier.value.in_([ident, ns_scoped[1]] if ns_scoped else [ident]),
        )
        for row in session.scalars(id_stmt.limit(p.pool_limit)):
            e, via = _canonical_hit(session, session.get(model, row.entity_id), p)
            if e is None or (p.status is not None and e.status != p.status):
                continue
            _add(
                hits,
                rtype,
                e,
                {"kind": "EXACT_EXTERNAL_ID", "field": f"identifier:{row.scheme}", "exact": True, "detail": row.value}
                | ({"via_merged": via} if via else {}),
            )

        if not p.include_merged:  # old codes / source ids of merged records resolve to the canonical record
            old = session.scalars(
                select(model)
                .where(
                    model.merged_into_id.is_not(None),
                    or_(model.external_id == ident, getattr(model, CODE_ATTR[rtype]) == raw),
                )
                .order_by(model.id)
                .limit(p.pool_limit)
            )
            for m in old:
                c, via = _canonical_hit(session, m, p)
                if c is not None and (p.status is None or c.status == p.status):
                    _add(
                        hits,
                        rtype,
                        c,
                        {
                            "kind": "EXACT_EXTERNAL_ID",
                            "field": "former record",
                            "exact": True,
                            "detail": raw,
                            "via_merged": via,
                        },
                    )

        if not norm:
            continue
        # 3. exact normalised name / alias
        for e in run([model.name_normalized == norm]):
            _add(hits, rtype, e, {"kind": "EXACT_NAME", "field": "name", "exact": True, "detail": e.name})
        al = session.scalars(
            select(EntityAlias)
            .where(EntityAlias.entity_type == rtype, EntityAlias.alias_normalized == norm)
            .limit(p.pool_limit)
        )
        for a in al:
            e, via = _canonical_hit(session, session.get(model, a.entity_id), p)
            if e is not None and (p.status is None or e.status == p.status):
                _add(
                    hits,
                    rtype,
                    e,
                    {"kind": "EXACT_ALIAS", "field": "alias", "exact": True, "detail": a.alias}
                    | ({"via_merged": via} if via else {}),
                )
        # 4. prefix (indexed range)
        like = like_escape(norm)
        pref = run([model.name_normalized.like(f"{like}%", escape="\\")])
        if len(pref) >= p.pool_limit:
            truncated = True
        for e in pref:
            _add(hits, rtype, e, {"kind": "PREFIX", "field": "name", "exact": False, "detail": e.name})
        # 5. substring (bounded)
        if len(norm) >= 3:
            sub = run([model.name_normalized.like(f"%{like}%", escape="\\")])
            if len(sub) >= p.pool_limit:
                truncated = True
            for e in sub:
                _add(hits, rtype, e, {"kind": "CONTAINS", "field": "name", "exact": False, "detail": e.name})
            # alias substring
            for a in session.scalars(
                select(EntityAlias)
                .where(EntityAlias.entity_type == rtype, EntityAlias.alias_normalized.like(f"%{like}%", escape="\\"))
                .limit(p.pool_limit)
            ):
                e = session.get(model, a.entity_id)
                if (
                    e is not None
                    and (p.include_merged or e.merged_into_id is None)
                    and (p.status is None or e.status == p.status)
                ):
                    _add(hits, rtype, e, {"kind": "CONTAINS", "field": "alias", "exact": False, "detail": a.alias})
        # 6. bounded fuzzy
        if p.fuzzy_enabled:
            tokens = [t for t in name_tokens(raw) if len(t) >= 3][:4]
            pool: dict[int, Entity] = {}
            for tok in tokens:
                tl = like_escape(tok[: max(3, len(tok) - 1)])
                for e in run([model.name_normalized.like(f"%{tl}%", escape="\\")], limit=p.pool_limit):
                    pool.setdefault(e.id, e)
            if len(pool) >= p.pool_limit * max(1, len(tokens)):
                truncated = True
            for e in pool.values():
                if (rtype, e.id) in hits and any(not r["kind"] == "FUZZY" for r in hits[(rtype, e.id)]["reasons"]):
                    continue
                score = similarity(raw, e.name)
                if score >= p.fuzzy_threshold:
                    _add(
                        hits,
                        rtype,
                        e,
                        {"kind": "FUZZY", "field": "name", "exact": False, "score": score, "detail": e.name},
                    )

    results = []
    for hit in hits.values():
        reasons = sorted(hit["reasons"], key=lambda r: (REASON_RANK[r["kind"]], -r.get("score", 1.0)))
        best = reasons[0]
        entity = hit["entity"]
        canonical = None
        if entity.merged_into_id is not None:
            c = canonical_of(session, entity)
            canonical = {"type": hit["type"].value, "id": c.id, "code": code_of(c), "name": c.name}
        results.append(
            {
                "type": hit["type"].value,
                "id": entity.id,
                "code": code_of(entity),
                "name": entity.name,
                "status": entity.status.value,
                "country_or_region": entity.country_or_region if hasattr(entity, "country_or_region") else None,
                "category": getattr(entity, "category", None),
                "merged_into": canonical,
                "source_namespace": entity.source_namespace,
                "external_id": entity.external_id,
                "updated_at": entity.updated_at,
                "version": entity.version,
                "match_reasons": reasons,
                "exact": any(r["exact"] for r in reasons),
                "approximate": not any(r["exact"] for r in reasons),
                "best_reason": best["kind"],
                "_rank": (REASON_RANK[best["kind"]], -best.get("score", 1.0), core_name(entity.name), entity.id),
            }
        )
    if p.sort == "name":
        results.sort(key=lambda r: (r["name"].casefold(), r["id"]))
    elif p.sort == "updated":
        results.sort(key=lambda r: (r["updated_at"], r["id"]), reverse=True)
    else:
        results.sort(key=lambda r: r["_rank"])
    for r in results:
        r.pop("_rank")
    total = len(results)
    return {
        "items": results[p.offset : p.offset + p.limit],
        "total": total,
        "limit": p.limit,
        "offset": p.offset,
        "truncated": truncated,
        "notes": [
            "Approximate matches are suggestions only; they never imply the records are the same entity.",
            *(
                ["Candidate pools hit the configured bound; refine the query for complete results."]
                if truncated
                else []
            ),
        ],
    }
