import { useCallback, useState } from "react";
import { when } from "../../components/risk/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Needs, Notice } from "../../components/registry/shared";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { can } from "../../lib/registryApi";
import { riskApi } from "../../lib/riskApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useRiskContext } from "./RiskLayout";

const LIMIT = 20;

export function RiskRunsPage() {
  const { me } = useRiskContext();
  const [offset, setOffset] = useState(0);
  const { data, error, loading, reload } = useRegistryApi(useCallback((s: AbortSignal) => riskApi.runs(LIMIT, offset, s), [offset]));
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ApiError>();
  const [done, setDone] = useState<string>();
  const allowed = can(me?.role, "reviewer", me?.writes_enabled ?? false);

  async function start() {
    setBusy(true);
    setProblem(undefined);
    setDone(undefined);
    try {
      const r = await riskApi.startRun({ scope: "NETWORK", reason: "Requested from the supplier risk page" });
      setDone(`Run #${r.id} queued. Refresh to see its outcome; it assesses every active supplier within the configured limits.`);
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-4">
      <div className="panel flex flex-wrap items-center gap-3 p-3 text-sm">
        <button type="button" className="btn" disabled={busy || !allowed} onClick={() => void start()}>
          {busy ? "Queuing…" : "Run network assessment"}
        </button>
        <button type="button" className="btn" onClick={reload}>
          Refresh
        </button>
        <span className="text-xs text-(--color-muted)">Runs are bounded and run inside the API process. Only one run per scope is active at a time; unchanged suppliers keep their existing assessment.</span>
      </div>
      {!allowed ? <Needs role="reviewer" me={me} /> : null}
      {problem ? <Notice tone="danger">{problem.message}</Notice> : null}
      {done ? <Notice>{done}</Notice> : null}
      {loading ? <LoadingState label="Loading runs…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No assessment runs yet">Run a network assessment to produce the first assessments.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Assessment runs</caption>
            <thead>
              <tr>
                <th>Run</th>
                <th>Scope</th>
                <th>Status</th>
                <th>Queued</th>
                <th className="num">Examined</th>
                <th className="num">Assessed</th>
                <th className="num">Partial</th>
                <th className="num">Insufficient</th>
                <th className="num">New</th>
                <th className="num">Unchanged</th>
                <th className="num">Failed</th>
                <th className="num">Skipped</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((r) => (
                <tr key={r.id}>
                  <td>#{r.id}</td>
                  <td>{titleCase(r.scope_type)}</td>
                  <td>{titleCase(r.status)}</td>
                  <td>{when(r.queued_at)}</td>
                  <td className="num">{r.examined}</td>
                  <td className="num">{r.assessed}</td>
                  <td className="num">{r.partial}</td>
                  <td className="num">{r.insufficient_evidence}</td>
                  <td className="num">{r.created}</td>
                  <td className="num">{r.unchanged}</td>
                  <td className="num">{r.failed}</td>
                  <td className="num">{r.skipped}</td>
                  <td className="text-xs">
                    {r.error ? <div className="text-(--color-danger)">{r.error}</div> : null}
                    {r.failures.map((f, i) => (
                      <div key={i}>
                        Supplier {f.supplier_id}: {f.error}
                      </div>
                    ))}
                    {r.truncated ? <div>Truncated: {r.truncation_reasons.join(", ")}</div> : null}
                    {r.requested_by} · {r.methodology_version ?? "—"}
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
