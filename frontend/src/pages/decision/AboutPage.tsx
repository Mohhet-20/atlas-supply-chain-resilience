import { useCallback } from "react";
import { Statement, words } from "../../components/decision/Bits";
import { ErrorState, LoadingState } from "../../components/States";
import { decisionApi } from "../../lib/decisionApi";
import { useRegistryApi } from "../../lib/useRegistry";

export function AboutPage() {
  const fh = useCallback((s: AbortSignal) => decisionApi.health(s), []);
  const fm = useCallback((s: AbortSignal) => decisionApi.metrics(s), []);
  const h = useRegistryApi(fh);
  const m = useRegistryApi(fm);
  if ((h.loading && !h.data) || (m.loading && !m.data)) return <LoadingState label="Loading metrics and limits…" />;
  if (h.error) return <ErrorState error={h.error} onRetry={h.reload} />;
  if (m.error) return <ErrorState error={m.error} onRetry={m.reload} />;
  if (!h.data || !m.data) return null;
  return (
    <div className="space-y-5">
      <Statement>{h.data.statements.decision_support ?? "Decision cases compare results and record a human decision. Nothing is executed."}</Statement>
      <section className="panel space-y-2 p-4" aria-labelledby="lim-h">
        <h2 id="lim-h" className="m-0 text-base">Versions and limits</h2>
        <p className="m-0 text-sm">Algorithm {h.data.algorithm_version} · metric registry {h.data.metric_registry_version} · writes enabled: {h.data.write_api_enabled ? "yes" : "no"}</p>
        <p className="m-0 text-xs text-(--color-muted)">{Object.entries(h.data.limits).map(([k, v]) => `${words(k)} ${v}`).join(" · ")}</p>
      </section>
      <section className="panel space-y-3 p-4" aria-labelledby="met-h">
        <h2 id="met-h" className="m-0 text-base">Metrics</h2>
        <div className="overflow-x-auto">
          <table className="tbl"><caption className="sr-only">Metric registry</caption>
            <thead><tr><th>Metric</th><th>Category</th><th>Unit</th><th>Direction</th><th>Time basis</th><th>Definition</th><th>If missing</th></tr></thead>
            <tbody>{m.data.metrics.map((x) => <tr key={x.id}><td>{x.name}</td><td>{x.category}</td><td>{x.unit}</td><td>{x.direction === "min" ? "lower is better" : x.direction === "max" ? "higher is better" : "neutral (reported as changed)"}</td><td className="text-xs">{x.time_basis}</td><td className="text-xs">{x.definition}</td><td className="text-xs">{x.missing}</td></tr>)}</tbody>
          </table>
        </div>
      </section>
      <section className="panel space-y-2 p-4" aria-labelledby="un-h">
        <h2 id="un-h" className="m-0 text-base">Metrics that are not available</h2>
        <ul className="m-0 list-disc pl-5 text-sm">{m.data.unavailable.map((u) => <li key={u.id}><strong>{u.name}:</strong> {u.reason}</li>)}</ul>
      </section>
      <section className="panel space-y-2 p-4" aria-labelledby="ns-h">
        <h2 id="ns-h" className="m-0 text-base">Not supported</h2>
        <ul className="m-0 list-disc pl-5 text-sm">{h.data.not_supported.map((n) => <li key={n}>{n}</li>)}</ul>
      </section>
    </div>
  );
}
