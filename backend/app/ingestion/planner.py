"""Builds an ingestion plan from raw bytes. Pure with respect to the database: it only READS.

The same plan drives both preview (nothing is written) and commit (the plan is applied), so
preview can never disagree with commit logic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ingestion import entities as ent
from app.ingestion.constants import (
    CANONICAL_PROFILE_REF,
    EXTERNAL_ID_PATTERN,
    NAMESPACE_PATTERN,
    NORMALIZATION_VERSION,
)
from app.ingestion.fieldspecs import SPECS, FieldSpec, spec_map
from app.ingestion.normalize import apply_transforms, clean_text, coerce_field, normalize_external_id
from app.ingestion.parsers import ParsedFile, RawRow, parse_csv, parse_json
from app.ingestion.profile import ResolvedProfile
from app.ingestion.rules import Finding
from app.models.enums import (
    EntityKind,
    IngestPolicy,
    ProductLinkType,
    RelationshipType,
    Severity,
)
from app.models.product import ProductLink
from app.models.relationship import SupplyRelationship
from app.models.source import DataSource

CHUNK = 400


@dataclass
class PlanRow:
    index: int
    line: int | None
    external_id: str | None = None
    values: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, int] = field(default_factory=dict)  # canonical ref field -> resolved entity id
    findings: list[Finding] = field(default_factory=list)
    action: str = "REJECT"  # CREATE | UPDATE | UNCHANGED | REJECT | DUPLICATE
    existing_id: int | None = None
    changed: list[str] = field(default_factory=list)
    fingerprint: str | None = None

    @property
    def blocked(self) -> bool:
        return any(f.blocking for f in self.findings)


@dataclass
class Plan:
    source: DataSource
    entity: EntityKind
    fmt: str
    policy: IngestPolicy
    profile: ResolvedProfile
    content_sha256: str
    content_bytes: int
    file_name: str | None
    parsed: ParsedFile
    rows: list[PlanRow] = field(default_factory=list)
    file_findings: list[Finding] = field(default_factory=list)
    fingerprint: str = ""
    plan_digest: str = ""

    def all_findings(self) -> list[Finding]:
        out = list(self.file_findings)
        for r in self.rows:
            out.extend(r.findings)
        return out

    @property
    def has_blocking(self) -> bool:
        return any(f.blocking for f in self.file_findings) or any(r.blocked for r in self.rows)

    def counts(self) -> dict[str, int]:
        c = {
            "total_rows": len(self.rows),
            "to_create": 0,
            "to_update": 0,
            "unchanged": 0,
            "rejected": 0,
            "duplicates": 0,
        }
        for r in self.rows:
            if r.action == "CREATE":
                c["to_create"] += 1
            elif r.action == "UPDATE":
                c["to_update"] += 1
            elif r.action == "UNCHANGED":
                c["unchanged"] += 1
            elif r.action == "DUPLICATE":
                c["duplicates"] += 1
            else:
                c["rejected"] += 1
        return c


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def compute_fingerprint(*, namespace, entity, fmt, content_sha, profile, policy, encoding, delimiter) -> str:
    return _sha(
        {
            "ns": namespace,
            "entity": entity.value,
            "fmt": fmt,
            "content": content_sha,
            "profile": [profile.ref, profile.digest],
            "policy": policy.value,
            "norm": NORMALIZATION_VERSION,
            "enc": encoding,
            "delim": delimiter,
        }
    )


# ----------------------------------------------------------------------------- mapping


def _lookup(row: RawRow, source: str, ci: bool) -> tuple[bool, Any]:
    if source in row.values:
        return True, row.values[source]
    if ci:
        low = source.lower()
        for k, v in row.values.items():
            if k.lower() == low:
                return True, v
    return False, None


def _map_row(raw: RawRow, entity: EntityKind, profile: ResolvedProfile) -> PlanRow:
    row = PlanRow(raw.index, raw.line)
    specs = spec_map(entity)
    d = profile.definition
    null_tokens = {t.lower() for t in d.null_tokens}
    for name, mapping in d.columns.items():
        spec = specs.get(name)
        if spec is None:
            continue
        present, rawval = _lookup(raw, mapping.source, profile.case_insensitive_headers)
        if present and isinstance(rawval, (list, dict)):
            row.findings.append(
                Finding(
                    "DQ013",
                    f"Field '{mapping.source}' contains an array/object; only scalar values are mapped.",
                    field=name,
                )
            )
            continue
        text = clean_text(rawval) if present else None
        if text is not None and text.lower() in null_tokens:
            text = None
        if text is not None and mapping.value_map:
            lowered = {k.strip().lower(): v for k, v in mapping.value_map.items()}
            text = lowered.get(text.lower(), text)
        text = apply_transforms(text, mapping.transform)
        if text is None and mapping.default is not None:
            text = clean_text(mapping.default)
        eff = replace(spec, required=spec.required and name == "external_id")  # required checked on CREATE only
        value, fnd = coerce_field(eff, text, d.date_format)
        if name == "external_id":
            value = normalize_external_id(value)
        row.findings.extend(fnd)
        row.values[name] = value
    row.external_id = row.values.get("external_id")
    for f in row.findings:
        f.row_index, f.source_line, f.external_id = row.index, row.line, row.external_id
    return row


def _apply_unit_rules(row: PlanRow, profile: ResolvedProfile) -> None:
    for fld, rule in profile.definition.unit_rules.items():
        value = row.values.get(fld)
        if value is None:
            continue
        unit = row.values.get(rule.unit_field)
        if unit is None:
            row.findings.append(
                Finding("DQ016", f"'{fld}' has a value but its unit '{rule.unit_field}' is missing.", field=fld)
            )
            continue
        factor = rule.factors.get(str(unit).strip().upper())
        if factor is None:
            row.findings.append(
                Finding(
                    "DQ016",
                    f"Unit '{str(unit)[:20]}' is not covered by the profile's unit rules.",
                    field=rule.unit_field,
                    observed=str(unit),
                    suggestion="Add the unit to unit_rules.factors or correct the source.",
                )
            )
            continue
        row.values[fld] = value * factor
        row.values[rule.unit_field] = rule.target_unit


# ----------------------------------------------------------------------------- references


class RefResolver:
    def __init__(self, session: Session, default_source: DataSource) -> None:
        self.session = session
        self.default = default_source
        self._sources: dict[str, DataSource | None] = {default_source.namespace: default_source}
        self._cache: dict[tuple[EntityKind, str, str], int | None] = {}

    def _source(self, ns: str) -> DataSource | None:
        if ns not in self._sources:
            self._sources[ns] = self.session.scalar(select(DataSource).where(DataSource.namespace == ns))
        return self._sources[ns]

    def parse(self, text: str) -> tuple[str, str] | None:
        ns, sep, ext = text.partition(":")
        if not sep:
            ns, ext = self.default.namespace, text
        if not NAMESPACE_PATTERN.match(ns) or not EXTERNAL_ID_PATTERN.match(ext):
            return None
        return ns, ext

    def resolve(self, entity: EntityKind, text: str) -> int | None | str:
        parsed = self.parse(text)
        if parsed is None:
            return "invalid"
        ns, ext = parsed
        key = (entity, ns, ext)
        if key not in self._cache:
            src = self._source(ns)
            found = None
            if src is not None:
                model = ent.MODEL[entity]
                found = self.session.scalar(select(model.id).where(model.source_id == src.id, model.external_id == ext))
            self._cache[key] = found
        return self._cache[key]


def _resolve_refs(row: PlanRow, entity: EntityKind, resolver: RefResolver) -> None:
    specs = spec_map(entity)
    for name, spec in specs.items():
        if spec.kind != "ref":
            continue
        text = row.values.get(name)
        if text is None:
            continue
        assert spec.ref_entity is not None  # noqa: S101
        res = resolver.resolve(spec.ref_entity, text)
        if res == "invalid":
            row.findings.append(
                Finding(
                    "DQ009",
                    f"Reference '{name}' is not a valid 'namespace:id' or 'id'.",
                    field=name,
                    observed=text,
                    row_index=row.index,
                    source_line=row.line,
                    external_id=row.external_id,
                )
            )
        elif res is None:
            row.findings.append(
                Finding(
                    "DQ009",
                    f"Referenced {spec.ref_entity.value.lower()} '{text[:60]}' does not exist.",
                    field=name,
                    observed=text,
                    row_index=row.index,
                    source_line=row.line,
                    external_id=row.external_id,
                    suggestion="Import the referenced records first (suppliers, facilities, products), then relationships.",  # noqa: E501
                )
            )
        else:
            row.refs[name] = int(res)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- planning


def _existing_map(session: Session, entity: EntityKind, source: DataSource, ids: list[str]) -> dict[str, Any]:
    model = ent.MODEL[entity]
    found: dict[str, Any] = {}
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i : i + CHUNK]
        for obj in session.scalars(select(model).where(model.source_id == source.id, model.external_id.in_(chunk))):
            found[obj.external_id] = obj
    return found


def _taken_codes(session: Session, entity: EntityKind, codes: list[str]) -> set[str]:
    model = ent.MODEL[entity]
    attr = getattr(model, ent.CODE_ATTR[entity])
    taken: set[str] = set()
    for i in range(0, len(codes), CHUNK):
        taken.update(session.scalars(select(attr).where(attr.in_(codes[i : i + CHUNK]))))
    return taken


def _name_candidates(
    session: Session, entity: EntityKind, source: DataSource, rows: list[PlanRow]
) -> dict[str, list[str]]:
    """Existing records (other source ids) sharing a case-folded name: candidates only."""
    if entity not in (EntityKind.SUPPLIER, EntityKind.FACILITY, EntityKind.PRODUCT):
        return {}
    model = ent.MODEL[entity]
    names = sorted({str(r.values["name"]).casefold() for r in rows if r.values.get("name")})
    out: dict[str, list[str]] = {}
    for i in range(0, len(names), CHUNK):
        q = select(func.lower(model.name), getattr(model, ent.CODE_ATTR[entity])).where(
            func.lower(model.name).in_(names[i : i + CHUNK])
        )
        for lname, code in session.execute(q):
            out.setdefault(lname, []).append(code)
    return out


def _merged(existing: Any, row: PlanRow, key: str) -> Any:
    new = row.values.get(key)
    if new is not None:
        return new
    return getattr(existing, key, None) if existing is not None else None


def _endpoint_key(entity: EntityKind, row: PlanRow, existing: Any) -> tuple | None:
    if entity not in (EntityKind.RELATIONSHIP, EntityKind.PRODUCT_LINK):
        return None
    refs = ent.REF_ATTR[entity]
    type_field = "relationship_type" if entity is EntityKind.RELATIONSHIP else "link_type"
    typ = row.values.get(type_field) or getattr(existing, type_field, None)
    parts = [typ.value if hasattr(typ, "value") else typ]
    for ref_name, attr in refs.items():
        parts.append(row.refs.get(ref_name, getattr(existing, attr, None) if existing is not None else None))
    return tuple(parts)


def _validate_cross(entity: EntityKind, row: PlanRow, existing: Any, creating: bool) -> None:
    def add(rule: str, msg: str, field_: str | None = None, sugg: str | None = None) -> None:
        row.findings.append(
            Finding(
                rule,
                msg,
                field=field_,
                suggestion=sugg,
                row_index=row.index,
                source_line=row.line,
                external_id=row.external_id,
            )
        )

    specs = SPECS[entity]
    if creating:
        for spec in specs:
            if spec.required and spec.name != "external_id" and row.values.get(spec.name) is None:
                add("DQ001", f"Required field '{spec.name}' is empty or missing.", spec.name)
    for a, b in ent.PAIRS.get(entity, ()):
        if any(f.field in (a, b) for f in row.findings):
            continue  # a field already failed validation; do not add a misleading pairing error
        va, vb = _merged(existing, row, a), _merged(existing, row, b)
        if (va is None) != (vb is None):
            add("DQ017", f"'{a}' and '{b}' must be provided together.", b if va is not None else a)
    v = row.values
    if entity is EntityKind.RELATIONSHIP:
        typ = v.get("relationship_type") or getattr(existing, "relationship_type", None)
        sup = "supplier_ref" in row.refs or v.get("supplier_ref")
        src = "source_facility_ref" in row.refs or v.get("source_facility_ref")
        if creating or v.get("relationship_type"):
            if typ is RelationshipType.SUPPLIES and (not sup or src):
                add("DQ012", "SUPPLIES requires supplier_ref and forbids source_facility_ref.", "relationship_type")
            elif typ is RelationshipType.TRANSFERS_TO and (not src or sup):
                add("DQ012", "TRANSFERS_TO requires source_facility_ref and forbids supplier_ref.", "relationship_type")
        if row.refs.get("source_facility_ref") and row.refs.get("source_facility_ref") == row.refs.get(
            "target_facility_ref"
        ):
            add("DQ017", "A facility cannot transfer to itself.", "target_facility_ref")
    elif entity is EntityKind.PRODUCT_LINK:
        typ = v.get("link_type") or getattr(existing, "link_type", None)
        sup = v.get("supplier_ref")
        fac = v.get("facility_ref")
        if creating or v.get("link_type"):
            if typ is ProductLinkType.SUPPLIES_PRODUCT and (not sup or fac):
                add("DQ012", "SUPPLIES_PRODUCT requires supplier_ref and forbids facility_ref.", "link_type")
            elif typ in (ProductLinkType.PRODUCES, ProductLinkType.STORES) and (not fac or sup):
                add("DQ012", f"{typ.value} requires facility_ref and forbids supplier_ref.", "link_type")
        start, end = _merged(existing, row, "effective_from"), _merged(existing, row, "effective_to")
        if start and end and end < start:
            add("DQ017", "effective_to is before effective_from.", "effective_to")
    if existing is not None and not creating and entity in ent.IMMUTABLE_ON_UPDATE:
        for name in ent.IMMUTABLE_ON_UPDATE[entity]:
            if name in ent.REF_ATTR.get(entity, {}):
                if name in row.refs and row.refs[name] != getattr(existing, ent.REF_ATTR[entity][name]):
                    add(
                        "DQ012",
                        f"'{name}' of an existing record cannot be changed by ingestion.",
                        name,
                        "Create a new record with a new external id instead.",
                    )
            elif v.get(name) is not None and v[name] != getattr(existing, name):
                add("DQ012", f"'{name}' of an existing record cannot be changed by ingestion.", name)


def _changed_fields(entity: EntityKind, row: PlanRow, existing: Any) -> list[str]:
    changed: list[str] = []
    for key in ent.APPLY_FIELDS[entity]:
        new = row.values.get(key)
        if new is not None and new != getattr(existing, key):
            changed.append(key)
    for ref_name, attr in ent.REF_ATTR.get(entity, {}).items():
        if entity in ent.IMMUTABLE_ON_UPDATE and ref_name in ent.IMMUTABLE_ON_UPDATE[entity]:
            continue
        if ref_name in row.refs and row.refs[ref_name] != getattr(existing, attr):
            changed.append(ref_name)
    return changed


def build_plan(
    session: Session,
    *,
    source: DataSource,
    entity: EntityKind,
    fmt: str,
    data: bytes,
    profile: ResolvedProfile,
    policy: IngestPolicy,
    encoding: str,
    delimiter: str,
    file_name: str | None,
    max_rows: int,
) -> Plan:
    content_sha = hashlib.sha256(data).hexdigest()
    parsed = (
        parse_csv(data, encoding=encoding, delimiter=delimiter, max_rows=max_rows)
        if fmt == "CSV"
        else parse_json(data, encoding=encoding, max_rows=max_rows)
    )
    plan = Plan(source, entity, fmt, policy, profile, content_sha, len(data), file_name, parsed)
    plan.fingerprint = compute_fingerprint(
        namespace=source.namespace,
        entity=entity,
        fmt=fmt,
        content_sha=content_sha,
        profile=profile,
        policy=policy,
        encoding=encoding,
        delimiter=delimiter,
    )
    plan.file_findings.extend(parsed.file_findings)
    if parsed.fatal:
        plan.plan_digest = _sha([plan.fingerprint, "fatal"])
        return plan

    d = profile.definition
    ci = profile.case_insensitive_headers
    cols_l = {c.lower() if ci else c for c in parsed.columns}
    used = set()
    for name, mapping in d.columns.items():
        key = mapping.source.lower() if ci else mapping.source
        if key in cols_l:
            used.add(key)
        elif fmt == "CSV":
            spec = spec_map(entity).get(name)
            if spec and spec.required and mapping.default is None:
                plan.file_findings.append(
                    Finding(
                        "DQ001",
                        f"Required column '{mapping.source}' (for '{name}') is not in the file header.",
                        field=name,
                        suggestion="Fix the mapping profile or the file header.",
                    )
                )
            elif spec:
                plan.file_findings.append(
                    Finding(
                        "DQ020",
                        f"Optional column '{mapping.source}' (for '{name}') is absent; value is unknown.",
                        field=name,
                    )
                )
    unknown = [c for c in parsed.columns if (c.lower() if ci else c) not in used]
    if unknown and d.unknown_field_policy != "ignore":
        sev = Severity.ERROR if d.unknown_field_policy == "reject" else Severity.WARNING
        names = ", ".join(u[:30] for u in unknown[:15]) + (" …" if len(unknown) > 15 else "")
        plan.file_findings.append(
            Finding(
                "DQ008",
                f"Unmapped source fields: {names}.",
                severity=sev,
                suggestion="Map them in the profile or set unknown_field_policy to ignore.",
            )
        )

    # --- per-row normalization
    resolver = RefResolver(session, source)
    for raw in parsed.rows:
        if raw.error is not None:
            r = PlanRow(raw.index, raw.line)
            raw.error.row_index, raw.error.source_line = raw.index, raw.line
            r.findings.append(raw.error)
            plan.rows.append(r)
            continue
        row = _map_row(raw, entity, profile)
        _apply_unit_rules(row, profile)
        _resolve_refs(row, entity, resolver)
        plan.rows.append(row)

    # --- identity: duplicates inside the batch
    first_seen: dict[str, PlanRow] = {}
    folded: dict[str, PlanRow] = {}
    for row in plan.rows:
        eid = row.external_id
        if eid is None:
            continue
        if eid in first_seen:
            row.findings.append(
                Finding(
                    "DQ010",
                    f"External id '{eid}' already appears at row {first_seen[eid].index}; this row is ignored.",
                    field="external_id",
                    row_index=row.index,
                    source_line=row.line,
                    external_id=eid,
                )
            )
            row.action = "DUPLICATE"
        else:
            first_seen[eid] = row
            other = folded.get(eid.casefold())
            if other is not None:
                row.findings.append(
                    Finding(
                        "DQ011",
                        f"'{eid}' differs from '{other.external_id}' (row {other.index}) only by letter case. Identifiers are case-sensitive, so both are kept as distinct records.",  # noqa: E501
                        field="external_id",
                        row_index=row.index,
                        source_line=row.line,
                        external_id=eid,
                        suggestion="Review whether the source system treats these as one identifier.",
                    )
                )
            folded.setdefault(eid.casefold(), row)

    # --- existing records, codes, candidates
    unique_rows = [r for r in plan.rows if r.external_id and r.action != "DUPLICATE"]
    existing = _existing_map(session, entity, source, [r.external_id for r in unique_rows])  # type: ignore[misc]
    create_codes = [
        ent.external_code(source.namespace, r.external_id) for r in unique_rows if r.external_id not in existing
    ]  # type: ignore[arg-type]
    taken = _taken_codes(session, entity, create_codes)
    seen_endpoints: dict[tuple, int] = {}

    for row in unique_rows:
        ex = existing.get(row.external_id)
        creating = ex is None
        _validate_cross(entity, row, ex, creating)
        if creating:
            code = ent.external_code(source.namespace, row.external_id)  # type: ignore[arg-type]
            if code in taken:
                row.findings.append(
                    Finding(
                        "DQ014",
                        f"Registry code '{code}' is already used by a record from another origin.",
                        field="external_id",
                        row_index=row.index,
                        source_line=row.line,
                        external_id=row.external_id,
                    )
                )
            key = _endpoint_key(entity, row, None)
            if key is not None and not row.blocked:
                if key in seen_endpoints:
                    row.findings.append(
                        Finding(
                            "DQ015",
                            f"Same endpoints as row {seen_endpoints[key]} in this batch.",
                            row_index=row.index,
                            source_line=row.line,
                            external_id=row.external_id,
                        )
                    )
                else:
                    seen_endpoints[key] = row.index
                    dup = _existing_edge(session, entity, key)
                    if dup:
                        row.findings.append(
                            Finding(
                                "DQ015",
                                "An identical link already exists in the registry under a different identifier.",
                                row_index=row.index,
                                source_line=row.line,
                                external_id=row.external_id,
                                suggestion="Update the existing record by re-using its external id.",
                            )
                        )
        else:
            row.existing_id = ex.id
    cands = _name_candidates(
        session, entity, source, [r for r in unique_rows if r.external_id not in existing]
    )  # placeholder filter
    for row in unique_rows:
        ex = existing.get(row.external_id)
        if row.blocked:
            row.action = "REJECT"
        elif ex is None:
            row.action = "CREATE"
            nm = row.values.get("name")
            if nm and str(nm).casefold() in cands:
                row.findings.append(
                    Finding(
                        "DQ011",
                        f"Another record already uses a similar name ({', '.join(cands[str(nm).casefold()][:3])}). Candidate only, not merged.",  # noqa: E501
                        field="name",
                        row_index=row.index,
                        source_line=row.line,
                        external_id=row.external_id,
                        suggestion="If these are the same real-world entity, review and link them manually.",
                    )
                )
        else:
            row.changed = _changed_fields(entity, row, ex)
            row.action = "UPDATE" if row.changed else "UNCHANGED"
        row.fingerprint = _sha({"v": {k: ent.json_safe(v) for k, v in row.values.items()}, "r": row.refs})
    for row in plan.rows:
        if row.blocked and row.action not in ("DUPLICATE",):
            row.action = "REJECT"

    for row in plan.rows:
        for f in row.findings:
            if f.row_index is None:
                f.row_index, f.source_line, f.external_id = row.index, row.line, row.external_id
    plan.plan_digest = _sha(
        [
            plan.fingerprint,
            [(r.index, r.action, r.existing_id, r.changed, r.fingerprint) for r in plan.rows],
            [(f.rule_id, f.row_index, f.field) for f in plan.file_findings],
        ]
    )
    return plan


def _existing_edge(session: Session, entity: EntityKind, key: tuple) -> bool:
    if entity is EntityKind.RELATIONSHIP:
        typ, sup, src, tgt = key
        q = select(SupplyRelationship.id).where(
            SupplyRelationship.relationship_type == typ, SupplyRelationship.target_facility_id == tgt
        )
        q = (
            q.where(SupplyRelationship.supplier_id == sup)
            if sup
            else q.where(SupplyRelationship.source_facility_id == src)
        )
        return session.scalar(q.limit(1)) is not None
    if entity is EntityKind.PRODUCT_LINK:
        typ, prod, sup, fac = key
        q = select(ProductLink.id).where(ProductLink.link_type == typ, ProductLink.product_id == prod)
        q = q.where(ProductLink.supplier_id == sup) if sup else q.where(ProductLink.facility_id == fac)
        return session.scalar(q.limit(1)) is not None
    return False


__all__ = ["CANONICAL_PROFILE_REF", "FieldSpec", "Plan", "PlanRow", "build_plan"]
