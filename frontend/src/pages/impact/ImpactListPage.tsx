import { useCallback, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Disclaimer, RunStatus, when } from "../../components/impact/Bits";
import { Pagination } from "../../components/Pagination";
import { Needs } from "../../components/registry/shared";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { impactApi } from "../../lib/impactApi";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useImpactContext } from "./ImpactLayout";

const LIMIT = 20;

export function ImpactListPage() {
  const { me } = useImpactContext();
  const nav = useNavigate();
  const [params] = useSearchParams();
  const [offset, setOffset] = useState(0);
  const [runId, setRunId] = useState(params.get("run_id") ?? "");
  const [picked, setPicked] = useState<number[]>([]);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<ApiError>();
  const fetcher = useCallback((s: AbortSignal) => impactApi.list(LIMIT, offset, s), [offset]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const canStart = can(me?.role, "editor", me?.writes_enabled ?? false);
  const toggle = (id: number) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : p.length < 5 ? [...p, id] : p));

  const start = async () => {
    const id = Number(runId);
    if (!Number.isInteger(id) || id < 1) return;
    setStarting(true);
    setStartError(undefined);
    try {
      const a = await impactApi.start(id);
      nav(`/impact/${a.id}`);
    } catch (e) {
      setStartError(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="space-y-4">
      <Disclaimer />
      <section className="panel space-y-2 p-3" aria-label="Start an analysis">
        <h2 className="m-0 text-base">Analyse a finished simulation run</h2>
        <div className="flex flex-wrap items-end gap-3 text-sm">
          <label className="flex flex-col gap-1">
            <span>Simulation run number</span>
            <input className="input w-36" inputMode="numeric" value={runId} onChange={(e) => setRunId(e.target.value)} />
          </label>
          <button type="button" className="btn" disabled={!canStart || starting || !/^\d+$/.test(runId)} onClick={() => void start()}>
            {starting ? "Starting…" : "Start analysis"}
          </button>
          <Link to="/scenarios/runs">Find a run</Link>
        </div>
        {!canStart ? <Needs role="editor" me={me} /> : null}
        {startError ? <p role="alert" className="m-0 text-sm text-(--color-danger)">{startError.message}</p> : null}
      </section>

      <div className="panel flex flex-wrap items-center gap-3 p-3 text-sm">
        <button type="button" className="btn" onClick={reload}>Refresh</button>
        <button type="button" className="btn" disabled={picked.length < 2} onClick={() => nav(`/impact/compare?ids=${picked.join(",")}`)}>
          Compare selected ({picked.length})
        </button>
        <span className="text-xs text-(--color-muted)">Select 2–5 finished analyses to compare.</span>
      </div>

      {loading && !data ? <LoadingState label="Loading analyses…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? (
        <EmptyState title="No cascading analyses yet">Start one from a completed simulation run. Analyses are stored permanently and never overwrite each other.</EmptyState>
      ) : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Cascading impact analyses</caption>
            <thead>
              <tr><th><span className="sr-only">Select</span></th><th>Analysis</th><th>Scenario / run</th><th>Status</th><th>Queued</th><th className="num">Entities</th><th>Notes</th></tr>
            </thead>
            <tbody>
              {data.items.map((a) => (
                <tr key={a.id}>
                  <td>
                    <input type="checkbox" aria-label={`Select analysis ${a.id}`} disabled={a.status !== "COMPLETED" && a.status !== "PARTIAL"} checked={picked.includes(a.id)} onChange={() => toggle(a.id)} />
                  </td>
                  <td><Link to={`/impact/${a.id}`}>#{a.id}</Link></td>
                  <td>{a.scenario_name ?? a.scenario_id} <span className="text-xs text-(--color-muted)">run #{a.run_id}</span></td>
                  <td><RunStatus value={a.status} /></td>
                  <td>{when(a.queued_at)}</td>
                  <td className="num">{a.summary ? a.summary.unique_entities : "—"}</td>
                  <td className="text-xs">
                    {a.error ? <div className="text-(--color-danger)">{a.error}</div> : null}
                    {a.truncated ? <div>Truncated: {a.truncation_reasons.join(", ")}</div> : null}
                    {a.requested_by}
                  </td>
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
