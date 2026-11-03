import { useCallback } from "react";
import { Statement, words } from "../../components/resilience/Bits";
import { ErrorState, LoadingState } from "../../components/States";
import { resilienceApi } from "../../lib/resilienceApi";
import { useRegistryApi } from "../../lib/useRegistry";

export function AboutPage() {
  const fh = useCallback((s: AbortSignal) => resilienceApi.health(s), []);
  const fc = useCallback((s: AbortSignal) => resilienceApi.catalogue(s), []);
  const h = useRegistryApi(fh);
  const c = useRegistryApi(fc);
  if ((h.loading && !h.data) || (c.loading && !c.data)) return <LoadingState label="Loading objectives and limits…" />;
  if (h.error) return <ErrorState error={h.error} onRetry={h.reload} />;
  if (c.error) return <ErrorState error={c.error} onRetry={c.reload} />;
  if (!h.data || !c.data) return null;
  return (
    <div className="space-y-5">
      <Statement>{Object.values(c.data.statements)[0] ?? "Resilience results are decision support only."}</Statement>
      <section className="panel space-y-2 p-4" aria-labelledby="solver-h">
        <h2 id="solver-h" className="m-0 text-base">Solver</h2>
        <p className="m-0 text-sm">{h.data.solver.name} · algorithm {h.data.algorithm_version} · <strong>{words(h.data.solver.type)}</strong>: plans are enumerated on a share grid and checked independently, so a result is the best of the plans examined, not a proof of global optimality. Writes enabled: {h.data.write_api_enabled ? "yes" : "no"}.</p>
        <p className="m-0 text-xs text-(--color-muted)">Limits: {Object.entries(h.data.limits).map(([k, v]) => `${words(k)} ${v}`).join(" · ")}</p>
      </section>
      <section className="panel space-y-3 p-4" aria-labelledby="obj-h">
        <h2 id="obj-h" className="m-0 text-base">Objectives</h2>
        <div className="overflow-x-auto">
          <table className="tbl"><caption className="sr-only">Objective definitions</caption>
            <thead><tr><th>Objective</th><th>Direction</th><th>Unit</th><th>Definition</th><th>If inputs are missing</th></tr></thead>
            <tbody>{c.data.objectives.map((o) => <tr key={o.id}><td>{o.name}</td><td>{o.direction === "min" ? "lower is better" : "higher is better"}</td><td>{o.unit}</td><td className="text-xs">{o.definition}</td><td className="text-xs">{o.missing}</td></tr>)}</tbody>
          </table>
        </div>
      </section>
      <section className="panel space-y-2 p-4" aria-labelledby="con-h">
        <h2 id="con-h" className="m-0 text-base">Constraints</h2>
        <ul className="m-0 list-disc pl-5 text-sm">{c.data.constraints.map((k) => <li key={k.id}><strong>{k.kind}</strong> · {words(k.id)}: {k.description}</li>)}</ul>
      </section>
      <section className="panel space-y-2 p-4" aria-labelledby="ns-h">
        <h2 id="ns-h" className="m-0 text-base">Not supported</h2>
        <ul className="m-0 list-disc pl-5 text-sm">{h.data.not_supported.map((n) => <li key={n}>{n}</li>)}</ul>
      </section>
    </div>
  );
}
