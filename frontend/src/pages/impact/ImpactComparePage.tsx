import { useCallback } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Disclaimer, num } from "../../components/impact/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { impactApi } from "../../lib/impactApi";
import { useRegistryApi } from "../../lib/useRegistry";

const COLS: [string, string][] = [
  ["unique_entities", "Unique entities"],
  ["suppliers_exposed", "Suppliers"],
  ["facilities_exposed", "Facilities"],
  ["products_exposed", "Products"],
  ["quantified", "Quantified"],
  ["unquantified", "Not quantified"],
  ["bottleneck_candidates", "Bottleneck candidates"],
];

export function ImpactComparePage() {
  const [params] = useSearchParams();
  const idsParam = params.get("ids") ?? "";
  const ids = idsParam.split(",").map(Number).filter((n) => Number.isInteger(n) && n > 0);
  const fetcher = useCallback((s: AbortSignal) => impactApi.compare(idsParam.split(",").map(Number).filter((n) => Number.isInteger(n) && n > 0), s), [idsParam]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  if (ids.length < 2) {
    return <EmptyState title="Choose analyses to compare">Select two to five finished analyses on the <Link to="/impact">Analyses</Link> tab.</EmptyState>;
  }
  return (
    <div className="space-y-4">
      <Disclaimer />
      {loading && !data ? <LoadingState label="Comparing analyses…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data ? (
        <>
          {!data.comparable ? (
            <div role="alert" className="panel space-y-1 border-(--color-danger) p-3 text-sm">
              <strong>These analyses are not directly comparable.</strong>
              <ul className="m-0 pl-5">{data.not_comparable_reasons.map((r) => <li key={r.code}>{r.text}</li>)}</ul>
            </div>
          ) : null}
          {data.warnings.length > 0 ? (
            <ul aria-label="Comparison warnings" className="m-0 space-y-1 pl-5 text-sm text-[#f5b942]">
              {data.warnings.map((w) => <li key={w.code}><strong>Warning:</strong> {w.text}</li>)}
            </ul>
          ) : null}
          <div className="panel overflow-x-auto">
            <table className="tbl">
              <caption className="sr-only">Headline comparison. The first analysis is the baseline.</caption>
              <thead>
                <tr><th>Measure</th>{data.runs.map((r) => <th key={r.analysis_id} className="num"><Link to={`/impact/${r.analysis_id}`}>Analysis #{r.analysis_id}</Link>{r.analysis_id === data.baseline_analysis_id ? " (baseline)" : ""}</th>)}</tr>
              </thead>
              <tbody>
                {COLS.map(([k, label]) => (
                  <tr key={k}><th scope="row">{label}</th>{data.runs.map((r) => <td key={r.analysis_id} className="num">{num((r as unknown as Record<string, number | null>)[k])}</td>)}</tr>
                ))}
                <tr><th scope="row">Mean input coverage</th>{data.runs.map((r) => <td key={r.analysis_id} className="num">{r.mean_input_coverage === null ? "—" : `${Math.round(r.mean_input_coverage * 100)}%`}</td>)}</tr>
                <tr><th scope="row">Hit a limit</th>{data.runs.map((r) => <td key={r.analysis_id} className="num">{r.truncated ? "Yes" : "No"}</td>)}</tr>
              </tbody>
            </table>
          </div>
          {data.comparable
            ? data.comparisons.map((c) => (
                <section key={c.other_analysis_id} className="panel space-y-2 p-3 text-sm" aria-label={`Changes in analysis ${c.other_analysis_id}`}>
                  <h3 className="m-0 text-base">Analysis #{c.other_analysis_id} compared with #{c.baseline_analysis_id}</h3>
                  <p className="m-0">
                    Newly exposed: <strong>{c.newly_exposed.total}</strong> · No longer exposed: <strong>{c.no_longer_exposed.total}</strong> · Exposed in both: <strong>{c.persistently_exposed}</strong> ·
                    Depth changes: <strong>{c.depth_changes.total}</strong> · Quantified changes: <strong>{c.quantified_changes.total}</strong>
                  </p>
                  {c.newly_exposed.items.length > 0 ? <p className="m-0 text-xs">New: {c.newly_exposed.items.map((i) => i.entity).join(", ")}</p> : null}
                  {c.no_longer_exposed.items.length > 0 ? <p className="m-0 text-xs">Gone: {c.no_longer_exposed.items.map((i) => i.entity).join(", ")}</p> : null}
                  <p className="m-0 text-xs text-(--color-muted)">{c.interpretation}</p>
                </section>
              ))
            : null}
          <p className="m-0 text-xs text-(--color-muted)">{data.note}</p>
        </>
      ) : null}
    </div>
  );
}
