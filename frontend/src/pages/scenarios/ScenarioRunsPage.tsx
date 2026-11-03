import { useCallback, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { StatusText, when } from "../../components/scenarios/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { scenarioApi } from "../../lib/scenarioApi";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 20;

export function ScenarioRunsPage() {
  const [params] = useSearchParams();
  const sid = Number(params.get("scenario_id")) || undefined;
  const nav = useNavigate();
  const [offset, setOffset] = useState(0);
  const [picked, setPicked] = useState<number[]>([]);
  const fetcher = useCallback((s: AbortSignal) => (sid ? scenarioApi.scenarioRuns(sid, LIMIT, offset, s) : scenarioApi.runs(LIMIT, offset, s)), [sid, offset]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const toggle = (id: number) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : p.length < 5 ? [...p, id] : p));
  return (
    <div className="space-y-4">
      <div className="panel flex flex-wrap items-center gap-3 p-3 text-sm">
        <button type="button" className="btn" onClick={reload}>Refresh</button>
        <button type="button" className="btn" disabled={picked.length < 2} onClick={() => nav(`/scenarios/compare?ids=${picked.join(",")}`)}>Compare selected ({picked.length})</button>
        <span className="text-xs text-(--color-muted)">Select 2–5 finished runs to compare. {sid ? `Showing runs of scenario ${sid}.` : ""}</span>
      </div>
      {loading && !data ? <LoadingState label="Loading runs…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No simulation runs yet">Run a validated scenario to produce results. History is kept even if the scenario is later edited or archived.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Simulation runs</caption>
            <thead><tr><th><span className="sr-only">Select</span></th><th>Run</th><th>Scenario</th><th>Status</th><th>Queued</th><th className="num">Affected</th><th className="num">Direct</th><th>Notes</th></tr></thead>
            <tbody>
              {data.items.map((r) => (
                <tr key={r.id}>
                  <td><input type="checkbox" aria-label={`Select run ${r.id}`} disabled={r.status !== "COMPLETED" && r.status !== "PARTIAL"} checked={picked.includes(r.id)} onChange={() => toggle(r.id)} /></td>
                  <td><Link to={`/scenarios/runs/${r.id}`}>#{r.id}</Link></td>
                  <td>{r.scenario_name ?? r.scenario_id} <span className="text-xs text-(--color-muted)">v{r.scenario_version}</span></td>
                  <td><StatusText value={r.status} /></td>
                  <td>{when(r.queued_at)}</td>
                  <td className="num">{r.status === "COMPLETED" || r.status === "PARTIAL" ? r.entities_affected : "—"}</td>
                  <td className="num">{r.status === "COMPLETED" || r.status === "PARTIAL" ? r.direct_count : "—"}</td>
                  <td className="text-xs">{r.error ? <div className="text-(--color-danger)">{r.error}</div> : null}{r.truncated ? <div>Truncated: {r.truncation_reasons.join(", ")}</div> : null}{r.requested_by}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      ) : null}
    </div>
  );
}
