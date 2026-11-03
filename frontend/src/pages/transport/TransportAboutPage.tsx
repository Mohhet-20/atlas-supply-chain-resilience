import { useCallback } from "react";
import { Statement } from "../../components/transport/Bits";
import { ErrorState, LoadingState } from "../../components/States";
import { transportApi } from "../../lib/transportApi";
import { useRegistryApi } from "../../lib/useRegistry";

export function TransportAboutPage() {
  const h = useRegistryApi(useCallback((s: AbortSignal) => transportApi.health(s), []));
  const o = useRegistryApi(useCallback((s: AbortSignal) => transportApi.objectives(s), []));
  const c = useRegistryApi(useCallback((s: AbortSignal) => transportApi.constraints(s), []));
  if ((h.loading && !h.data) || (o.loading && !o.data) || (c.loading && !c.data)) return <LoadingState label="Loading capabilities…" />;
  const err = h.error ?? o.error ?? c.error;
  if (err) return <ErrorState error={err} onRetry={() => { h.reload(); o.reload(); c.reload(); }} />;
  if (!h.data || !o.data || !c.data) return null;
  const x = h.data;
  return (
    <div className="space-y-5">
      <Statement>{x.statements.decision_support}</Statement>
      <section className="panel space-y-2 p-4" aria-labelledby="lim-h">
        <h2 id="lim-h" className="m-0 text-base font-semibold">Limits (enforced by the server)</h2>
        <ul className="m-0 space-y-1 pl-5 text-sm">
          <li>Up to {x.limits.max_lanes} lanes, {x.limits.max_hops} lanes per route, {x.limits.max_alternatives} alternatives, {x.limits.max_active_runs} active runs.</li>
          <li>Search budget {x.limits.max_expansions.toLocaleString("en-US")} expansions and {x.limits.time_budget_seconds} s; reaching it is reported and the result is labelled best-found.</li>
          <li>Measured reliability needs at least {x.limits.min_reliability_sample} completed deliveries on the lane; otherwise a documented prior or unknown.</li>
        </ul>
      </section>
      <section className="panel space-y-2 p-4" aria-labelledby="alg-h">
        <h2 id="alg-h" className="m-0 text-base font-semibold">Algorithms ({x.algorithm_version})</h2>
        <ul className="m-0 space-y-1 pl-5 text-sm">{x.algorithms.map((a) => <li key={a}>{a}</li>)}</ul>
      </section>
      <section className="panel space-y-2 p-4" aria-labelledby="obj-h">
        <h2 id="obj-h" className="m-0 text-base font-semibold">Objectives</h2>
        <ul className="m-0 space-y-1 pl-5 text-sm">{o.data.objectives.map((ob) => <li key={ob.name}><strong>{ob.name.toLowerCase().replace(/_/g, " ")}</strong>{ob.note ? ` — ${ob.note}` : ""}</li>)}</ul>
        <p className="m-0 text-xs text-(--color-muted)">{o.data.normalisation}</p>
      </section>
      <section className="panel space-y-2 p-4" aria-labelledby="con-h">
        <h2 id="con-h" className="m-0 text-base font-semibold">Hard constraints</h2>
        <table className="tbl"><caption className="sr-only">Constraints and how missing data is treated</caption><thead><tr><th>Constraint</th><th>Judged on</th><th>If data is missing</th></tr></thead>
          <tbody>{c.data.constraints.map((k) => <tr key={k.name}><td>{k.name}</td><td>{k.judged_on}</td><td className="text-xs">{k.missing_data}</td></tr>)}</tbody></table>
      </section>
      <section className="panel space-y-2 p-4" aria-labelledby="ns-h">
        <h2 id="ns-h" className="m-0 text-base font-semibold">Not supported</h2>
        <ul className="m-0 space-y-1 pl-5 text-sm">{x.not_supported.map((n) => <li key={n}>{n}</li>)}</ul>
      </section>
      <ul className="m-0 space-y-1 pl-5 text-xs text-(--color-muted)">{["cost", "duration", "reliability", "risk", "scenario", "tenancy"].map((k) => <li key={k}>{x.statements[k]}</li>)}</ul>
    </div>
  );
}
