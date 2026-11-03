import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { ResultMark, RunStatusMark, num, when, words } from "../../components/resilience/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { resilienceApi } from "../../lib/resilienceApi";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 15;
const RESULTS = ["completed", "completed_with_warnings", "infeasible", "timed_out", "invalid_input", "insufficient_data", "failed"];
const STRATS = ["WEIGHTED_SUM", "PARETO", "EPSILON_CONSTRAINT"];

export function RunHistoryPage() {
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState("");
  const [strategy, setStrategy] = useState("");
  const f = useCallback((s: AbortSignal) => resilienceApi.runs({ limit: LIMIT, offset, result_status: result || undefined, strategy: strategy || undefined }, s), [offset, result, strategy]);
  const { data, error, loading, reload } = useRegistryApi(f);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm">Result
          <select className="input ml-2" value={result} onChange={(e) => { setResult(e.target.value); setOffset(0); }}>
            <option value="">All</option>{RESULTS.map((r) => <option key={r} value={r}>{words(r)}</option>)}
          </select>
        </label>
        <label className="text-sm">Strategy
          <select className="input ml-2" value={strategy} onChange={(e) => { setStrategy(e.target.value); setOffset(0); }}>
            <option value="">All</option>{STRATS.map((r) => <option key={r} value={r}>{words(r)}</option>)}
          </select>
        </label>
      </div>
      {loading && !data ? <LoadingState label="Loading resilience runs…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No resilience runs yet">Configure an optimisation to create the first immutable, reproducible run.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Resilience run history</caption>
            <thead><tr><th>Run</th><th>Status</th><th>Result</th><th>Strategy</th><th>Demand</th><th>Alternatives</th><th>Approval</th><th>Queued</th></tr></thead>
            <tbody>
              {data.items.map((r) => (
                <tr key={r.run_uid}>
                  <td><Link to={`/resilience/runs/${r.run_uid}`} className="font-mono text-xs">{r.run_uid}</Link></td>
                  <td><RunStatusMark status={r.status} /></td><td><ResultMark status={r.result_status} /></td><td>{words(r.strategy)}</td>
                  <td className="tabular-nums">{num(r.quantity)} {r.unit}</td><td className="tabular-nums">{r.alternative_count} ({r.front_size ?? "—"} non-dominated)</td>
                  <td className="text-xs">{words(r.approval.state)}{r.approval.executed ? " · executed" : " · not executed"}</td><td>{when(r.queued_at)}</td>
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
