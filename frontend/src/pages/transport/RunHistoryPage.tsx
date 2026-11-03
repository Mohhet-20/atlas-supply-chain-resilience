import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { RunStatusMark, when } from "../../components/transport/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { transportApi } from "../../lib/transportApi";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 15;
const STATUSES = ["QUEUED", "RUNNING", "COMPLETED", "PARTIAL", "FAILED", "CANCELLED"];

export function RunHistoryPage() {
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const fetcher = useCallback((s: AbortSignal) => transportApi.runs({ limit: LIMIT, offset, status: status || undefined, kind: kind || undefined }, s), [offset, status, kind]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm">Status
          <select className="input ml-2" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
            <option value="">All</option>
            {STATUSES.map((s) => <option key={s} value={s}>{s.toLowerCase()}</option>)}
          </select>
        </label>
        <label className="text-sm">Kind
          <select className="input ml-2" value={kind} onChange={(e) => { setKind(e.target.value); setOffset(0); }}>
            <option value="">All</option>
            <option value="OPTIMIZE">Optimization</option>
            <option value="FEASIBILITY">Feasibility check</option>
          </select>
        </label>
      </div>
      {loading && !data ? <LoadingState label="Loading routing runs…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No routing runs yet">Plan a route to create the first immutable, traceable run.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Routing run history</caption>
            <thead><tr><th>Run</th><th>Kind</th><th>Status</th><th>Result</th><th>Objective</th><th>Facilities</th><th>Scenario</th><th>Queued</th></tr></thead>
            <tbody>
              {data.items.map((r) => (
                <tr key={r.run_uid}>
                  <td><Link to={`/transport/runs/${r.run_uid}`} className="font-mono text-xs">{r.run_uid}</Link></td>
                  <td>{r.kind.toLowerCase()}</td>
                  <td><RunStatusMark status={r.status} /></td>
                  <td>{r.result_status?.replace(/_/g, " ") ?? "—"}</td>
                  <td>{r.objective?.toLowerCase().replace(/_/g, " ") ?? "—"}</td>
                  <td>#{r.origin_facility_id} → #{r.destination_facility_id}</td>
                  <td>{r.scenario_id ? `#${r.scenario_id}` : "—"}</td>
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
