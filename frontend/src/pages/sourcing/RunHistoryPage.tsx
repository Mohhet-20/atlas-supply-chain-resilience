import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { ApprovalBadge, RunStatusMark, num, when, words } from "../../components/sourcing/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { sourcingApi } from "../../lib/sourcingApi";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 15;
const RESULTS = ["feasible", "partial", "infeasible", "incomplete", "not_solved"];

export function RunHistoryPage() {
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState("");
  const [kind, setKind] = useState("");
  const f = useCallback((s: AbortSignal) => sourcingApi.runs({ limit: LIMIT, offset, result_status: result || undefined, kind: kind || undefined }, s), [offset, result, kind]);
  const { data, error, loading, reload } = useRegistryApi(f);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm">Result
          <select className="input ml-2" value={result} onChange={(e) => { setResult(e.target.value); setOffset(0); }}>
            <option value="">All</option>
            {RESULTS.map((r) => <option key={r} value={r}>{words(r)}</option>)}
          </select>
        </label>
        <label className="text-sm">Kind
          <select className="input ml-2" value={kind} onChange={(e) => { setKind(e.target.value); setOffset(0); }}>
            <option value="">All</option>
            <option value="OPTIMIZE">Optimisation</option>
            <option value="COMPARE">Strategy comparison</option>
          </select>
        </label>
      </div>
      {loading && !data ? <LoadingState label="Loading sourcing runs…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No sourcing runs yet">Define a requirement to create the first immutable, traceable run.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Sourcing run history</caption>
            <thead><tr><th>Run</th><th>Status</th><th>Result</th><th>Objective</th><th>Demand</th><th>Suppliers</th><th>Approval</th><th>Queued</th></tr></thead>
            <tbody>
              {data.items.map((r) => (
                <tr key={r.run_uid}>
                  <td><Link to={`/sourcing/runs/${r.run_uid}`} className="font-mono text-xs">{r.run_uid}</Link>{r.hypothetical ? <span className="ml-1 text-xs">(what-if)</span> : null}</td>
                  <td><RunStatusMark status={r.status} /></td>
                  <td>{words(r.result_status)}</td>
                  <td>{words(r.objective)}</td>
                  <td className="tabular-nums">{num(r.demand)} {r.unit}</td>
                  <td className="tabular-nums">{r.supplier_count ?? "—"}</td>
                  <td><ApprovalBadge approval={r.approval} /></td>
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
