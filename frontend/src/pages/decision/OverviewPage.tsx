import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { StatusMark, Statement, when, words } from "../../components/decision/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { decisionApi } from "../../lib/decisionApi";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 15;
const STATUSES = ["DRAFT", "READY", "EVALUATED", "UNDER_REVIEW", "APPROVED", "REJECTED", "SUPERSEDED"];

export function OverviewPage() {
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const f = useCallback((s: AbortSignal) => decisionApi.cases({ limit: LIMIT, offset, status: status || undefined, q: q.trim() || undefined }, s), [offset, status, q]);
  const { data, error, loading, reload } = useRegistryApi(f);
  const fh = useCallback((s: AbortSignal) => decisionApi.health(s), []);
  const health = useRegistryApi(fh);
  return (
    <div className="space-y-4">
      {health.data ? <Statement>{health.data.statements.decision_support ?? "Decision cases compare existing results. Approval records a decision; it executes nothing."}</Statement> : null}
      {health.data ? (
        <p className="m-0 text-xs text-(--color-muted)">
          {STATUSES.map((s) => `${words(s)}: ${health.data?.cases_by_status[s] ?? 0}`).join(" · ")}
        </p>
      ) : null}
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm">Status
          <select className="input ml-2" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
            <option value="">All</option>{STATUSES.map((s) => <option key={s} value={s}>{words(s)}</option>)}
          </select>
        </label>
        <label className="text-sm">Title contains
          <input className="input ml-2" value={q} maxLength={100} onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
        </label>
      </div>
      {loading && !data ? <LoadingState label="Loading decision cases…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No decision cases yet">Create a case from finished sourcing, resilience or scenario runs to compare plans against a baseline.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Decision cases</caption>
            <thead><tr><th>Case</th><th>Title</th><th>Status</th><th>Evaluation</th><th>Alternatives</th><th>Reviewer</th><th>Updated</th></tr></thead>
            <tbody>
              {data.items.map((c) => (
                <tr key={c.case_uid}>
                  <td><Link to={`/decision/cases/${c.case_uid}`} className="font-mono text-xs">{c.case_uid}</Link></td>
                  <td>{c.title}</td><td><StatusMark status={c.status} /></td>
                  <td className="text-xs">{c.current_result_status ? words(c.current_result_status) : "not evaluated"}</td>
                  <td className="tabular-nums">{c.alternative_count ?? "—"}</td><td>{c.reviewer ?? "unassigned"}</td><td>{when(c.updated_at)}</td>
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
