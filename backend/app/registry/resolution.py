"""Entity resolution: bounded candidate generation, transparent scoring and persistence of duplicate candidates.

Scores are *similarity/evidence* scores (sums of configurable points, clipped to [0, 1]); they are not
probabilities. Tiers (STRONG / POSSIBLE / INSUFFICIENT) are triage categories, never proof of identity, and
nothing in this module merges, changes or deletes a record.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import Actor
from app.core.errors import NotFoundError
from app.models.enums import CandidateStatus, CandidateTier, RegistryType, RunStatus
from app.models.registry import CandidateReview, DuplicateCandidate, EntityAlias, EntityIdentifier, ResolutionRun
from app.models.types import utcnow
from app.registry import audit
from app.registry.config import ResolutionConfig, config_hash
from app.registry.entities import MODELS, Entity, active_entities, canonical_of, code_of
from app.registry.normalize import GENERIC_TOKENS, core_name, name_tokens, normalize_address, normalize_name

TIER_RANK = {CandidateTier.INSUFFICIENT: 0, CandidateTier.POSSIBLE: 1, CandidateTier.STRONG: 2}


@dataclass
class PairResult:
    score: float
    tier: CandidateTier
    hard_conflict: bool
    signals: list[dict[str, Any]]
    families: list[str]
    digest: str

    def evidence(self, cfg: ResolutionConfig) -> dict[str, Any]:
        return {
            "rule_version": cfg.version,
            "score": self.score,
            "score_meaning": "similarity score from summed evidence points; not a probability",
            "tier": self.tier.value,
            "hard_conflict": self.hard_conflict,
            "families": self.families,
            "signals": self.signals,
            "thresholds": {"strong": cfg.strong_threshold, "possible": cfg.possible_threshold},
        }


@dataclass
class Context:
    """Per-run lookups loaded once (avoids per-pair queries)."""

    idents: dict[tuple[RegistryType, int], list[tuple[int, str, str, bool]]] = field(default_factory=dict)
    aliases: dict[tuple[RegistryType, int], set[str]] = field(default_factory=dict)
    canonical_owner: dict[int, int] = field(default_factory=dict)


def load_context(session: Session, rtype: RegistryType, ids: list[int]) -> Context:
    ctx = Context()
    if not ids:
        return ctx
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for row in session.scalars(
            select(EntityIdentifier).where(EntityIdentifier.entity_type == rtype, EntityIdentifier.entity_id.in_(chunk))
        ):
            ctx.idents.setdefault((rtype, row.entity_id), []).append(
                (row.source_id, row.scheme, row.value, row.authoritative)
            )
        for row in session.scalars(
            select(EntityAlias).where(EntityAlias.entity_type == rtype, EntityAlias.entity_id.in_(chunk))
        ):
            ctx.aliases.setdefault((rtype, row.entity_id), set()).add(row.alias_normalized)
    return ctx


def _generic(name: str) -> bool:
    toks = name_tokens(name)
    return not toks or all(t in GENERIC_TOKENS for t in toks) or len("".join(toks)) < 4


def _sim(a: str, b: str) -> float:
    ta, tb = a.split(), b.split()
    if not ta or not tb:
        return 0.0
    jac = len(set(ta) & set(tb)) / len(set(ta) | set(tb))
    ratio = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(jac, ratio)


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _present(v: Any) -> bool:
    return v is not None and str(v).strip() != ""


def score_pair(rtype: RegistryType, a: Entity, b: Entity, ctx: Context, cfg: ResolutionConfig) -> PairResult:
    """Deterministic evidence scoring. Missing attributes never count as matches."""
    pts = cfg.points
    signals: list[dict[str, Any]] = []

    def add(key: str, family: str, points: float, detail: str) -> None:
        signals.append({"signal": key, "family": family, "points": round(points, 4), "detail": detail})

    hard_conflict = False
    # ---- identifiers
    ia = ctx.idents.get((rtype, a.id), [])
    ib = ctx.idents.get((rtype, b.id), [])
    auth_schemes = set(cfg.authoritative_schemes)
    code_schemes = set(cfg.code_schemes)

    def by_scheme(items, schemes, only_flagged=False):
        out: dict[str, set[str]] = {}
        for _src, scheme, value, flagged in items:
            if scheme in schemes or (only_flagged and flagged):
                out.setdefault(scheme, set()).add(value)
        return out

    def authoritative(items):
        out: dict[str, set[str]] = {}
        for _src, scheme, value, flagged in items:
            if scheme in auth_schemes or flagged:
                out.setdefault(scheme, set()).add(value)
        return out

    aa, ab = authoritative(ia), authoritative(ib)
    for scheme in sorted(set(aa) & set(ab)):
        shared = aa[scheme] & ab[scheme]
        if shared:
            add(
                "authoritative_id_match",
                "identifier",
                pts.authoritative_id_match,
                f"{scheme} {sorted(shared)[0]} on both",
            )
        else:
            hard_conflict = True
            add("identifier_conflict", "conflict", pts.identifier_conflict, f"{scheme} values differ (authoritative)")
    if rtype is RegistryType.PRODUCT:
        ca, cb = by_scheme(ia, code_schemes), by_scheme(ib, code_schemes)
        for scheme in sorted(set(ca) & set(cb)):
            if scheme in auth_schemes:
                continue  # already handled as authoritative
            shared = ca[scheme] & cb[scheme]
            if shared:
                add("code_match", "identifier", pts.code_match, f"{scheme} {sorted(shared)[0]} on both")

    # ---- names
    na, nb = a.name_normalized, b.name_normalized
    aliases_a = ctx.aliases.get((rtype, a.id), set())
    aliases_b = ctx.aliases.get((rtype, b.id), set())
    if na and na == nb:
        f = cfg.generic_name_factor if _generic(a.name) else 1.0
        add(
            "name_exact",
            "name",
            pts.name_exact * f,
            "normalized names are identical" + (" (generic name, reduced)" if f < 1 else ""),
        )
    else:
        ca_, cb_ = core_name(a.name), core_name(b.name)
        if ca_ and ca_ == cb_:
            f = cfg.generic_name_factor if _generic(a.name) else 1.0
            add("core_name_exact", "name", pts.core_name_exact * f, "names match after removing legal forms")
        else:
            s = _sim(ca_, cb_)
            if s >= cfg.name_similarity_min:
                scaled = (s - cfg.name_similarity_min) / (1 - cfg.name_similarity_min)
                f = cfg.generic_name_factor if _generic(a.name) or _generic(b.name) else 1.0
                add("name_similarity", "name", pts.name_similarity * scaled * f, f"name similarity {s:.2f}")
    if rtype is RegistryType.SUPPLIER and _present(a.legal_name) and _present(b.legal_name):
        if normalize_name(a.legal_name) == normalize_name(b.legal_name):
            add("legal_name_exact", "name", pts.legal_name_exact, "legal names are identical")
    if (na and na in aliases_b) or (nb and nb in aliases_a) or (aliases_a & aliases_b):
        if not any(s["signal"] == "name_exact" for s in signals):
            add(
                "alias_match",
                "name",
                pts.alias_match,
                "a name or alias of one record equals a name or alias of the other",
            )

    # ---- country / address / category (location + attributes)
    if hasattr(a, "country_or_region"):
        ca_c, cb_c = normalize_name(a.country_or_region), normalize_name(b.country_or_region)
        if ca_c and cb_c:
            if ca_c == cb_c:
                add("country_match", "location", pts.country_match, f"same country/region ({a.country_or_region})")
            elif cfg.cross_country.get(rtype.value, "penalize") == "penalize":
                mult = cfg.facility_country_conflict_multiplier if rtype is RegistryType.FACILITY else 1.0
                add(
                    "country_conflict",
                    "conflict",
                    pts.country_conflict * mult,
                    f"{a.country_or_region} vs {b.country_or_region}",
                )
    if hasattr(a, "address") and _present(a.address) and _present(b.address):
        s = _sim(normalize_address(a.address), normalize_address(b.address))
        if s >= cfg.address_similarity_min:
            add("address_similarity", "location", pts.address_similarity, f"address similarity {s:.2f}")
        elif s < 0.35:
            add("address_conflict", "conflict", pts.address_conflict, "addresses differ")
    if hasattr(a, "category") and _present(a.category) and _present(b.category):
        if normalize_name(a.category) == normalize_name(b.category):
            add("category_match", "attributes", pts.category_match, "same category")

    # ---- entity specific
    if rtype is RegistryType.FACILITY:
        if a.facility_type == b.facility_type:
            add("type_match", "attributes", pts.type_match, f"both {a.facility_type.value}")
        else:
            add("type_conflict", "conflict", pts.type_conflict, f"{a.facility_type.value} vs {b.facility_type.value}")
        if a.owner_supplier_id and b.owner_supplier_id:
            if a.owner_supplier_id == b.owner_supplier_id:
                add("owner_match", "relationship", pts.owner_match, "same owning supplier")
            else:
                add("owner_conflict", "conflict", pts.owner_conflict, "different owning suppliers")
        if None not in (a.latitude, a.longitude, b.latitude, b.longitude):
            km = _haversine_km(a.latitude, a.longitude, b.latitude, b.longitude)
            if km <= cfg.geo_near_km:
                add("geo_near", "location", pts.geo_near, f"coordinates {km:.2f} km apart")
            elif km >= cfg.geo_far_km:
                add("geo_far", "conflict", pts.geo_far, f"coordinates {km:.0f} km apart")
    if rtype is RegistryType.PRODUCT:
        if a.manufacturer_supplier_id and b.manufacturer_supplier_id:
            if a.manufacturer_supplier_id == b.manufacturer_supplier_id:
                add("manufacturer_match", "relationship", pts.manufacturer_match, "same manufacturer")
            else:
                add("manufacturer_conflict", "conflict", pts.manufacturer_conflict, "different manufacturers")
        if _present(a.unit_of_measure) and _present(b.unit_of_measure):
            if normalize_name(a.unit_of_measure) != normalize_name(b.unit_of_measure):
                add("uom_conflict", "conflict", pts.uom_conflict, f"{a.unit_of_measure} vs {b.unit_of_measure}")

    raw = sum(s["points"] for s in signals)
    score = round(min(1.0, max(0.0, raw)), 4)
    families = sorted({s["family"] for s in signals if s["points"] > 0})
    if score >= cfg.strong_threshold and len(families) >= cfg.min_corroborating_families and not hard_conflict:
        tier = CandidateTier.STRONG
    elif score >= cfg.possible_threshold:
        tier = CandidateTier.POSSIBLE
    else:
        tier = CandidateTier.INSUFFICIENT
    if score >= cfg.strong_threshold and tier is CandidateTier.POSSIBLE and not hard_conflict:
        signals.append(
            {
                "signal": "tier_cap",
                "family": "note",
                "points": 0.0,
                "detail": (
                    f"score is high but fewer than {cfg.min_corroborating_families} independent evidence families agree"
                ),
            }
        )
    digest = hashlib.sha256(
        json.dumps(
            [
                tier.value,
                hard_conflict,
                [(s["signal"], s["points"]) for s in sorted(signals, key=lambda s: s["signal"])],
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return PairResult(score, tier, hard_conflict, signals, families, digest)


# ------------------------------------------------------------------- blocking


def blocking_keys(rtype: RegistryType, e: Entity, ctx: Context, cfg: ResolutionConfig) -> set[str]:
    keys: set[str] = set()
    if e.name_normalized:
        keys.add("x:" + e.name_normalized)
    c = core_name(e.name)
    if c:
        keys.add("c:" + c)
    for tok in name_tokens(e.name, drop_generic=True)[:3]:
        if len(tok) >= 3:
            keys.add("t:" + tok)
    for al in ctx.aliases.get((rtype, e.id), set()):
        keys.add("x:" + al)
        keys.add("c:" + " ".join(t for t in al.split() if t))
    auth = set(cfg.authoritative_schemes) | set(cfg.code_schemes)
    for _src, scheme, value, flagged in ctx.idents.get((rtype, e.id), []):
        if scheme in auth or flagged:
            keys.add(f"i:{scheme}:{value}")
    if rtype is RegistryType.FACILITY and e.latitude is not None and e.longitude is not None:
        keys.add(f"g:{round(e.latitude, 1)},{round(e.longitude, 1)}")
    return keys


# ------------------------------------------------------------------------ run


def _pair_key(x: int, y: int) -> tuple[int, int]:
    return (x, y) if x < y else (y, x)


def run_resolution(
    session: Session,
    rtype: RegistryType,
    cfg: ResolutionConfig,
    actor: Actor,
    *,
    entity_ids: list[int] | None = None,
    request_id: str | None = None,
    max_entities: int = 20_000,
) -> ResolutionRun:
    """Generate or refresh duplicate candidates. Bounded by block size, pair count, candidate count and time."""
    started = time.monotonic()
    chash = config_hash(cfg)
    run = ResolutionRun(
        run_uid=f"RES-{uuid.uuid4().hex[:16].upper()}",
        entity_type=rtype,
        config_version=cfg.version,
        config_hash=chash,
        status=RunStatus.COMPLETED,
        actor=actor.name,
        started_at=utcnow(),
    )
    session.add(run)
    session.flush()

    entities = active_entities(session, rtype)
    truncated: list[str] = []
    if len(entities) > max_entities:
        entities = entities[:max_entities]
        truncated.append(f"entity cap {max_entities} reached")
    by_id = {e.id: e for e in entities}
    ctx = load_context(session, rtype, sorted(by_id))
    subset = set(entity_ids) & set(by_id) if entity_ids is not None else None

    blocks: dict[str, list[int]] = {}
    for e in entities:
        for key in blocking_keys(rtype, e, ctx, cfg):
            blocks.setdefault(key, []).append(e.id)
    pairs: set[tuple[int, int]] = set()
    oversize = 0
    for key in sorted(blocks):
        members = blocks[key]
        if len(members) < 2:
            continue
        if len(members) > cfg.max_block_size:
            oversize += 1
            continue
        if subset is not None and not (subset & set(members)):
            continue
        iterator = (
            combinations(members, 2)
            if subset is None
            else ((a, b) for a, b in combinations(members, 2) if a in subset or b in subset)
        )
        for x, y in iterator:
            pairs.add(_pair_key(x, y))
            if len(pairs) >= cfg.max_pairs_per_run:
                truncated.append(f"pair cap {cfg.max_pairs_per_run} reached")
                break
        if len(pairs) >= cfg.max_pairs_per_run:
            break
    if oversize:
        truncated.append(f"{oversize} oversized block(s) skipped (max_block_size={cfg.max_block_size})")

    found = new = 0
    compared = 0
    for x, y in sorted(pairs):
        if time.monotonic() - started > cfg.max_runtime_seconds:
            truncated.append(f"time budget {cfg.max_runtime_seconds}s reached")
            break
        if found >= cfg.max_candidates_per_run:
            truncated.append(f"candidate cap {cfg.max_candidates_per_run} reached")
            break
        compared += 1
        a, b = by_id[x], by_id[y]
        result = score_pair(rtype, a, b, ctx, cfg)
        existing = session.scalar(
            select(DuplicateCandidate).where(
                DuplicateCandidate.entity_type == rtype,
                DuplicateCandidate.left_id == x,
                DuplicateCandidate.right_id == y,
            )
        )
        if result.tier is CandidateTier.INSUFFICIENT and existing is None:
            continue
        if result.tier is not CandidateTier.INSUFFICIENT:
            found += 1
        if existing is None:
            session.add(
                DuplicateCandidate(
                    entity_type=rtype,
                    left_id=x,
                    right_id=y,
                    rule_version=cfg.version,
                    config_hash=chash,
                    score=result.score,
                    tier=result.tier,
                    hard_conflict=result.hard_conflict,
                    evidence=result.evidence(cfg),
                    evidence_digest=result.digest,
                    status=CandidateStatus.NEW,
                    left_version=a.version,
                    right_version=b.version,
                    run_id=run.id,
                )
            )
            new += 1
        else:
            _refresh_existing(session, existing, result, cfg, chash, a, b, run.id)
    session.flush()

    run.entities_considered = len(by_id)
    run.blocks = len(blocks)
    run.pairs_compared = compared
    run.candidates_found = found
    run.candidates_new = new
    run.truncated_reason = "; ".join(truncated)[:200] or None
    run.status = RunStatus.TRUNCATED if truncated else RunStatus.COMPLETED
    run.finished_at = utcnow()
    run.duration_ms = int((time.monotonic() - started) * 1000)
    session.flush()
    audit.record(
        session,
        actor,
        "resolution.run",
        target_type=rtype.value,
        target_id=run.run_uid,
        details={
            "config_version": cfg.version,
            "pairs_compared": compared,
            "candidates_found": found,
            "candidates_new": new,
            "status": run.status.value,
            "entity_subset": len(subset) if subset is not None else None,
        },
        request_id=request_id,
    )
    return run


def _refresh_existing(session, cand, result, cfg, chash, a, b, run_id) -> None:
    previous_score, previous_tier = cand.score, cand.tier
    cand.rule_version = cfg.version
    cand.config_hash = chash
    cand.score = result.score
    cand.tier = result.tier
    cand.hard_conflict = result.hard_conflict
    old_digest = cand.evidence_digest
    cand.evidence = result.evidence(cfg)
    cand.evidence_digest = result.digest
    cand.left_version, cand.right_version = a.version, b.version
    cand.run_id = run_id
    if cand.status in (CandidateStatus.REJECTED, CandidateStatus.DEFERRED) and result.digest != old_digest:
        improved = (
            result.score >= previous_score + cfg.reopen_score_increase
            or TIER_RANK[result.tier] > TIER_RANK[previous_tier]
        )
        if improved and result.tier is not CandidateTier.INSUFFICIENT:
            prior = cand.status
            cand.status = CandidateStatus.REOPENED
            session.add(
                CandidateReview(
                    candidate_id=cand.id,
                    from_status=prior,
                    to_status=CandidateStatus.REOPENED,
                    actor="system",
                    authenticated=False,
                    reason="New evidence increased the match score; reopened automatically for human review.",
                    evidence_snapshot=cand.evidence,
                )
            )


def evaluate_pair(
    session: Session, rtype: RegistryType, left_id: int, right_id: int, cfg: ResolutionConfig
) -> PairResult:
    """Ad-hoc, non-persisting comparison of two records (e.g. to demonstrate 'insufficient evidence')."""
    model = MODELS[rtype]
    left, right = session.get(model, left_id), session.get(model, right_id)
    if left is None or right is None:
        raise NotFoundError("One of the records was not found.")
    ctx = load_context(session, rtype, [left_id, right_id])
    return score_pair(rtype, left, right, ctx, cfg)


__all__ = [
    "PairResult",
    "Context",
    "run_resolution",
    "score_pair",
    "evaluate_pair",
    "blocking_keys",
    "canonical_of",
    "code_of",
]
