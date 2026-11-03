import { useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Disclaimer, Loss, num } from "../../components/scenarios/Bits";
import { Notice } from "../../components/registry/shared";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { titleCase } from "../../lib/format";
import { scenarioApi } from "../../lib/scenarioApi";
import type * as K from "../../lib/scenarioTypes";
import { useRegistryApi } from "../../lib/useRegistry";

const parse = (raw: string | null) => (raw ?? "").split(",").map((x) => Number(x.trim())).filter((n) => Number.isInteger(n) && n > 0);

export function ScenarioComparePage() {
  const [params, setParams] = useSearchParams();
  const key = params.get("ids") ?? "";
  const ids = parse(key);
  const [text, setText] = useState(ids.join(", "));
  const fetcher = useCallback((s: AbortSignal) => (parse(key).length >= 2 ? scenarioApi.compare(parse(key), s) : Promise.resolve(null)), [key]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  return (
    <div className="space-y-4">
      <Disclaimer>Only completed or partial runs can be compared, using their stored results. Metrics are compared only when their definitions match; differences in engine, assumptions or network inputs are listed instead of being hidden.</Disclaimer>
      <form className="panel flex flex-wrap items-end gap-3 p-3" onSubmit={(e) => { e.preventDefault(); setParams({ ids: parse(text).join(",") }); }}>
        <label className="text-sm"><span className="mb-1 block text-xs text-(--color-muted)">Run ids (2–5, comma separated)</span><input className="field" value={text} onChange={(e) => setText(e.target.value)} /></label>
        <button type="submit" className="btn">Compare</button>
        <Link to="/scenarios/runs" className="text-sm">Pick from run history</Link>
      </form>
      {ids.length < 2 ? <EmptyState title="Choose runs to compare">Select two to five finished runs from the run history, or type their ids above.</EmptyState> : null}
      {ids.length >= 2 && loading ? <LoadingState label="Comparing runs…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data ? (
        <>
          {!data.comparable ? <Notice tone="danger"><strong>Not comparable.</strong> {data.not_comparable_reasons.map((r) => r.text).join(" ")}</Notice> : null}
          {data.warnings.map((w) => <Notice key={w.code} tone="warn">{w.text}</Notice>)}
          {data.assumption_differences.length ? <Notice>Assumptions that differ: {data.assumption_differences.map(titleCase).join(", ")}.</Notice> : null}
          <div className="panel overflow-x-auto">
            <table className="tbl">
              <caption className="sr-only">Run comparison</caption>
              <thead><tr><th>Metric</th>{data.runs.map((r) => <th key={r.run_id} className="num"><Link to={`/scenarios/runs/${r.run_id}`}>Run #{r.run_id}</Link><div className="text-xs font-normal text-(--color-muted)">scenario {r.scenario_id} v{r.scenario_version}{r.truncated ? " · truncated" : ""}</div></th>)}</tr></thead>
              <tbody>
                {([
                  ["Affected entities", (r) => r.entities_affected, data.metric_comparability.counts_and_sets],
                  ["Directly disrupted", (r) => r.direct, data.metric_comparability.counts_and_sets],
                  ["Indirect (potential)", (r) => r.indirect, data.metric_comparability.counts_and_sets],
                  ["Products", (r) => r.affected_products, data.metric_comparability.counts_and_sets],
                  ["Facilities", (r) => r.affected_facilities, data.metric_comparability.counts_and_sets],
                  ["Relationships", (r) => r.affected_relationships, data.metric_comparability.counts_and_sets],
                  ["Alternatives found", (r) => r.alternatives_total, data.metric_comparability.counts_and_sets],
                  ["Capacity at risk", (r) => (Object.keys(r.capacity_at_risk_by_unit).length ? Object.entries(r.capacity_at_risk_by_unit).map(([u, v]) => `${num(v)} ${u}`).join("; ") : "not calculable"), data.metric_comparability.capacity],
                  ["Missing-data conditions", (r) => r.missing_data_codes.join(", ") || "none", true],
                ] as [string, (r: K.Comparison["runs"][number]) => unknown, boolean][]).map(([label, fn, ok]) => (
                  <tr key={label}>
                    <th scope="row" className="font-normal">{label}{!ok ? <div className="text-xs text-[#f5b942]">not like-for-like</div> : null}</th>
                    {data.runs.map((r) => <td key={r.run_id} className="num">{String(fn(r) ?? "—")}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="panel overflow-x-auto">
            <h3 className="m-3 text-base">Entities ({data.entities.in_all_runs} in every run, {data.entities.only_in_some_runs} in only some)</h3>
            <table className="tbl">
              <caption className="sr-only">Entity-level comparison</caption>
              <thead><tr><th>Entity</th>{data.runs.map((r) => <th key={r.run_id}>Run #{r.run_id}</th>)}</tr></thead>
              <tbody>
                {data.entities.rows.map((e) => (
                  <tr key={`${e.entity_type}-${e.entity_id}`}>
                    <td>{e.entity_name}<div className="text-xs text-(--color-muted)">{titleCase(e.entity_type)}</div></td>
                    {data.runs.map((r) => {
                      const c = e.per_run[String(r.run_id)];
                      return <td key={r.run_id}>{c ? <>{titleCase(c.exposure_level)} · <Loss i={{ loss_pct: c.loss_pct, loss_pct_lower_bound: null, quantification: "UNQUANTIFIED", impact_kind: c.impact_kind as "DIRECT", added_lead_time_days: null }} /></> : <span className="text-(--color-muted)">not affected</span>}</td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
            {data.entities.truncated ? <p className="m-3 text-xs text-(--color-muted)">Only the first rows are shown ({data.entities.total} entities in total).</p> : null}
          </div>
          <p className="m-0 text-xs text-(--color-muted)">{data.note}</p>
        </>
      ) : null}
    </div>
  );
}
