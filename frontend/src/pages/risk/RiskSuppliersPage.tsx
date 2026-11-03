import { useCallback } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ClassBadge, Meter, NotAProbability, ReliabilityText, ScoreValue, StatusText, SyntheticNote } from "../../components/risk/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { titleCase } from "../../lib/format";
import { riskApi } from "../../lib/riskApi";
import { ASSESSMENT_STATUSES, CLASSES, type AssessmentStatus, type RiskClass } from "../../lib/riskTypes";
import { useDebounced } from "../../lib/useDebounced";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 25;
const SORTS: [string, string][] = [
  ["score_desc", "Score, highest first"],
  ["score_asc", "Score, lowest first"],
  ["coverage_desc", "Evidence coverage"],
  ["reliability_asc", "Reliability, lowest first"],
  ["computed_desc", "Most recently assessed"],
  ["name", "Name"],
];

export function RiskSuppliersPage() {
  const [sp, setSp] = useSearchParams();
  const q = sp.get("q") ?? "";
  const classification = (sp.get("classification") ?? "") as RiskClass | "";
  const status = (sp.get("assessment_status") ?? "") as AssessmentStatus | "";
  const region = sp.get("region") ?? "";
  const needsReview = sp.get("requires_review") === "true";
  const sort = sp.get("sort") ?? "score_desc";
  const offset = Number(sp.get("offset") ?? 0) || 0;
  const dq = useDebounced(q, 250);

  const set = (patch: Record<string, string | undefined>) => {
    const next = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    if (!("offset" in patch)) next.delete("offset");
    setSp(next, { replace: true });
  };

  const { data, error, loading, reload } = useRegistryApi(
    useCallback(
      (s: AbortSignal) =>
        riskApi.suppliers({ q: dq || undefined, classification, assessment_status: status, region: region || undefined, requires_review: needsReview || undefined, sort, limit: LIMIT, offset }, s),
      [dq, classification, status, region, needsReview, sort, offset],
    ),
  );
  const compareIds = (sp.get("pick") ?? "").split(",").filter(Boolean);
  const toggle = (id: number) => {
    const cur = new Set(compareIds);
    if (cur.has(String(id))) cur.delete(String(id));
    else if (cur.size < 6) cur.add(String(id));
    const next = new URLSearchParams(sp);
    if (cur.size) next.set("pick", [...cur].join(","));
    else next.delete("pick");
    setSp(next, { replace: true });
  };

  return (
    <div className="space-y-4">
      <form role="search" aria-label="Filter suppliers" className="panel flex flex-wrap items-end gap-3 p-3" onSubmit={(e) => e.preventDefault()}>
        <label className="text-xs text-(--color-muted)">
          Search
          <input className="field mt-1 block" type="search" value={q} placeholder="Name or code" onChange={(e) => set({ q: e.target.value || undefined })} />
        </label>
        <label className="text-xs text-(--color-muted)">
          Classification
          <select className="field mt-1 block" value={classification} onChange={(e) => set({ classification: e.target.value || undefined })}>
            <option value="">Any</option>
            {CLASSES.map((c) => (
              <option key={c} value={c}>
                {titleCase(c)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-(--color-muted)">
          Assessment status
          <select className="field mt-1 block" value={status} onChange={(e) => set({ assessment_status: e.target.value || undefined })}>
            <option value="">Any</option>
            {ASSESSMENT_STATUSES.map((c) => (
              <option key={c} value={c}>
                {titleCase(c)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-(--color-muted)">
          Sort
          <select className="field mt-1 block" value={sort} onChange={(e) => set({ sort: e.target.value })}>
            {SORTS.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-xs text-(--color-muted)">
          <input type="checkbox" checked={needsReview} onChange={(e) => set({ requires_review: e.target.checked ? "true" : undefined })} />
          Needs review
        </label>
        {region ? (
          <button type="button" className="btn" onClick={() => set({ region: undefined })}>
            Region: {region} ✕
          </button>
        ) : null}
        <Link
          className={`btn ml-auto no-underline ${compareIds.length < 2 ? "pointer-events-none opacity-45" : ""}`}
          aria-disabled={compareIds.length < 2}
          to={`/risk/compare?ids=${compareIds.join(",")}`}
        >
          Compare selected ({compareIds.length})
        </Link>
      </form>

      {loading ? <LoadingState label="Loading suppliers…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No suppliers match these filters">Clear a filter or search term to see more.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <>
          <SyntheticNote show={data.items.some((i) => i.contains_synthetic)} />
          <div className="panel overflow-x-auto">
            <table className="tbl">
              <caption className="sr-only">Supplier risk assessments</caption>
              <thead>
                <tr>
                  <th>
                    <span className="sr-only">Select</span>
                  </th>
                  <th>Supplier</th>
                  <th>Region</th>
                  <th>Status</th>
                  <th className="num">Risk score</th>
                  <th>Class</th>
                  <th>Evidence coverage</th>
                  <th>Reliability</th>
                  <th>Review</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((r) => (
                  <tr key={r.supplier_id}>
                    <td>
                      <input type="checkbox" aria-label={`Select ${r.name} for comparison`} checked={compareIds.includes(String(r.supplier_id))} onChange={() => toggle(r.supplier_id)} />
                    </td>
                    <td>
                      <Link to={`/risk/suppliers/${r.supplier_id}`}>{r.name}</Link>
                      <div className="text-xs text-(--color-muted)">{r.supplier_code}</div>
                    </td>
                    <td>{r.country_or_region}</td>
                    <td>
                      <StatusText status={r.assessment_status} />
                    </td>
                    <td className="num">
                      <ScoreValue score={r.risk_score} />
                    </td>
                    <td>
                      <ClassBadge value={r.classification} status={r.assessment_status} />
                      {r.override ? <div className="text-xs text-(--color-warn)">Analyst override: {titleCase(r.override.classification)}{r.override.effective ? "" : " (not in effect)"}</div> : null}
                    </td>
                    <td>{r.evidence_coverage === null ? "—" : <Meter value={r.evidence_coverage} label="Evidence coverage" />}</td>
                    <td>
                      <ReliabilityText value={r.reliability} band={r.reliability_band} />
                    </td>
                    <td>{r.requires_review && r.review_state === "UNREVIEWED" ? "Needs review" : r.review_state ? titleCase(r.review_state) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={(o) => set({ offset: o ? String(o) : undefined })} />
          </div>
          <p className="m-0 text-xs text-(--color-muted)">{data.ordering_note}</p>
          <NotAProbability />
        </>
      ) : null}
    </div>
  );
}
