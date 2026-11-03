import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { Disclaimer, StatusText, when } from "../../components/scenarios/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { titleCase } from "../../lib/format";
import { scenarioApi } from "../../lib/scenarioApi";
import type { ScenarioStatus, ScenarioType } from "../../lib/scenarioTypes";
import { useDebounced } from "../../lib/useDebounced";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 20;
const STATUSES: ScenarioStatus[] = ["DRAFT", "READY", "ARCHIVED"];
const TYPES: ScenarioType[] = ["SUPPLIER", "FACILITY", "RELATIONSHIP", "REGIONAL", "MULTI_ENTITY", "CUSTOM"];

export function ScenarioLibraryPage() {
  const [status, setStatus] = useState<ScenarioStatus | "">("");
  const [type, setType] = useState<ScenarioType | "">("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const dq = useDebounced(q.trim(), 300);
  const fetcher = useCallback(
    (s: AbortSignal) => scenarioApi.list({ status, scenario_type: type, q: dq || undefined, limit: LIMIT, offset }, s),
    [status, type, dq, offset],
  );
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  return (
    <div className="space-y-4">
      <Disclaimer />
      <div className="panel flex flex-wrap items-end gap-3 p-3">
        <label className="text-sm">
          <span className="mb-1 block text-xs text-(--color-muted)">Search by name</span>
          <input className="field" type="search" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} placeholder="Scenario name" />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs text-(--color-muted)">Status</span>
          <select className="field" value={status} onChange={(e) => { setStatus(e.target.value as ScenarioStatus | ""); setOffset(0); }}>
            <option value="">All (including archived)</option>
            {STATUSES.map((s) => <option key={s} value={s}>{titleCase(s)}</option>)}
          </select>
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs text-(--color-muted)">Type</span>
          <select className="field" value={type} onChange={(e) => { setType(e.target.value as ScenarioType | ""); setOffset(0); }}>
            <option value="">All types</option>
            {TYPES.map((s) => <option key={s} value={s}>{titleCase(s)}</option>)}
          </select>
        </label>
        <Link className="btn no-underline" to="/scenarios/new">New scenario</Link>
      </div>
      {loading ? <LoadingState label="Loading scenarios…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? (
        <EmptyState title="No scenarios yet">
          Nothing is pre-created. <Link to="/scenarios/new">Create a scenario</Link> from a template or from scratch, then choose real suppliers or facilities as targets.
        </EmptyState>
      ) : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Scenario library</caption>
            <thead>
              <tr><th>Name</th><th>Type</th><th>Status</th><th className="num">Version</th><th className="num">Runs</th><th>Owner</th><th>Updated</th></tr>
            </thead>
            <tbody>
              {data.items.map((s) => (
                <tr key={s.id}>
                  <td><Link to={`/scenarios/${s.id}`}>{s.name}</Link>{s.validation_current ? null : <span className="ml-2 text-xs text-(--color-muted)">not validated</span>}</td>
                  <td>{titleCase(s.scenario_type)}</td>
                  <td><StatusText value={s.status} /></td>
                  <td className="num">{s.version}</td>
                  <td className="num">{s.run_count ?? 0}</td>
                  <td>{s.owner}</td>
                  <td>{when(s.updated_at)}</td>
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
