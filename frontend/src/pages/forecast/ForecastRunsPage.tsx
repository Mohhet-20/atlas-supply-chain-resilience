import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { Disclaimer, PatternTag, ResultStatus } from "../../components/forecast/Bits";
import { RunStatus, when } from "../../components/inventory/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { forecastApi } from "../../lib/forecastApi";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 20;

export function ForecastRunsPage() {
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState("");
  const fetcher = useCallback((s: AbortSignal) => forecastApi.runs({ limit: LIMIT, offset, status: status || undefined }, s), [offset, status]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  return (
    <div className="space-y-4">
      <Disclaimer>Runs are stored permanently and never overwrite each other. A finished run is immutable.</Disclaimer>
      <label className="flex max-w-xs flex-col gap-1 text-sm">
        <span>Status</span>
        <select className="input" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
          <option value="">All</option>
          {["QUEUED", "RUNNING", "COMPLETED", "PARTIAL", "FAILED", "CANCELLED"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </label>
      {loading && !data ? <LoadingState label="Loading runs…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No forecast runs yet">Open the <Link to="/forecast/series">series explorer</Link> and run a forecast.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Forecast runs</caption>
            <thead><tr><th>Run</th><th>Status</th><th>Outcome</th><th>Model</th><th>Patterns</th><th>Origin</th><th>Horizon</th><th>Queued (UTC)</th></tr></thead>
            <tbody>
              {data.items.map((r) => (
                <tr key={r.id}>
                  <td><Link to={`/forecast/runs/${r.id}`}>#{r.id}</Link></td>
                  <td><RunStatus value={r.status} /></td>
                  <td><ResultStatus value={r.result_status} /></td>
                  <td>{r.selected_model ?? "—"}</td>
                  <td>{(r.patterns ?? []).map((p) => <PatternTag key={p} label={p} />)}</td>
                  <td>{r.origin ?? "—"}</td>
                  <td>{r.horizon} {r.frequency === "daily" ? "days" : "weeks"}</td>
                  <td>{when(r.queued_at)}</td>
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
