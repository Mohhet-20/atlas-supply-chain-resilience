import { useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { SeverityBadge, StatusPill } from "../../components/quality/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Pagination } from "../../components/Pagination";
import { titleCase } from "../../lib/format";
import { entityPath, qualityApi } from "../../lib/qualityApi";
import { DIMENSIONS } from "../../lib/qualityTypes";
import { useDebounced } from "../../lib/useDebounced";
import { useRegistryApi } from "../../lib/useRegistry";
import { FindingDetailPanel } from "./FindingDetailPanel";
import { useQualityContext } from "./QualityLayout";

const LIMIT = 25;
const STATUS_OPTIONS = [
  ["OPEN_ANY", "Open (any stage)"],
  ["", "All"],
  ["OPEN", "New"],
  ["ACKNOWLEDGED", "Acknowledged"],
  ["IN_REMEDIATION", "In remediation"],
  ["REOPENED", "Reopened"],
  ["RESOLVED", "Resolved"],
  ["EXCEPTION_ANY", "Accepted exceptions"],
] as const;

export function QualityFindingsPage() {
  const { me } = useQualityContext();
  const [params, setParams] = useSearchParams();
  const [text, setText] = useState(params.get("q") ?? "");
  const q = useDebounced(text.trim(), 300);
  const [selected, setSelected] = useState<number>();
  const [offset, setOffset] = useState(0);
  const status = params.get("status") ?? "OPEN_ANY";
  const apiStatus = status === "ALL" ? undefined : status;
  const minSeverity = params.get("min_severity") ?? "";
  const dimension = params.get("dimension") ?? "";
  const entityType = params.get("entity_type") ?? "";
  const ruleId = params.get("rule_id") ?? "";
  const batch = params.get("batch_id");

  function setFilter(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
    setOffset(0);
  }

  const fetcher = useCallback(
    (s: AbortSignal) =>
      qualityApi.findings(
        { status: apiStatus, min_severity: minSeverity || undefined, dimension: dimension || undefined, entity_type: entityType || undefined, rule_id: ruleId || undefined, batch_id: batch ? Number(batch) : undefined, q: q || undefined, limit: LIMIT, offset },
        s,
      ),
    [apiStatus, minSeverity, dimension, entityType, ruleId, batch, q, offset],
  );
  const list = useRegistryApi(fetcher);

  return (
    <div className="space-y-4">
      <form className="panel flex flex-wrap items-end gap-3 p-3 text-sm" role="search" aria-label="Filter findings" onSubmit={(e) => e.preventDefault()}>
        <label className="text-xs text-(--color-muted)">
          Status
          <select className="field mt-1 block" value={status} onChange={(e) => setFilter("status", e.target.value)}>
            {STATUS_OPTIONS.map(([v, l]) => (
              <option key={l} value={v || "ALL"}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-(--color-muted)">
          Minimum severity
          <select className="field mt-1 block" value={minSeverity} onChange={(e) => setFilter("min_severity", e.target.value)}>
            <option value="">Any</option>
            <option value="WARNING">Warning or worse</option>
            <option value="ERROR">Error or worse</option>
            <option value="CRITICAL">Critical</option>
          </select>
        </label>
        <label className="text-xs text-(--color-muted)">
          Dimension
          <select className="field mt-1 block" value={dimension} onChange={(e) => setFilter("dimension", e.target.value)}>
            <option value="">All</option>
            {DIMENSIONS.map((d) => (
              <option key={d} value={d}>
                {titleCase(d)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-(--color-muted)">
          Record type
          <select className="field mt-1 block" value={entityType} onChange={(e) => setFilter("entity_type", e.target.value)}>
            <option value="">All</option>
            {["SUPPLIER", "FACILITY", "PRODUCT", "RELATIONSHIP", "PRODUCT_LINK"].map((k) => (
              <option key={k} value={k}>
                {titleCase(k)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-(--color-muted)">
          Search message, identifier or rule
          <input className="field mt-1 block w-56" maxLength={80} value={text} onChange={(e) => { setText(e.target.value); setOffset(0); }} />
        </label>
        {ruleId || batch ? (
          <button type="button" className="btn" onClick={() => { setFilter("rule_id", ""); setFilter("batch_id", ""); }}>
            Clear {ruleId ? `rule ${ruleId}` : `batch ${batch}`} filter
          </button>
        ) : null}
      </form>

      {list.loading ? <LoadingState label="Loading findings…" /> : null}
      {list.error ? <ErrorState error={list.error} onRetry={list.reload} /> : null}
      {list.data && list.data.total === 0 ? (
        <EmptyState title="No findings match these filters">
          Findings appear after an evaluation or an import. If nothing has been evaluated yet, start a run from <Link to="/quality/runs">Evaluation runs</Link>.
        </EmptyState>
      ) : null}
      {list.data && list.data.total > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Data quality findings</caption>
            <thead>
              <tr>
                <th>Severity</th>
                <th>Status</th>
                <th>Finding</th>
                <th>Record</th>
                <th>Rule</th>
              </tr>
            </thead>
            <tbody>
              {list.data.items.map((f) => (
                <tr key={f.id} aria-selected={selected === f.id}>
                  <td>
                    <SeverityBadge severity={f.severity} />
                  </td>
                  <td>
                    <StatusPill status={f.status} />
                  </td>
                  <td className="max-w-md">
                    <button type="button" className="text-left text-(--color-accent) underline-offset-2 hover:underline" onClick={() => setSelected(f.id)}>
                      {f.message}
                    </button>
                  </td>
                  <td>
                    {f.entity_type && f.entity_id ? <Link to={entityPath(f.entity_type, f.entity_id)}>{f.entity_name ?? `${titleCase(f.entity_type)} ${f.entity_id}`}</Link> : f.batch_id ? `Batch ${f.batch_id}${f.row_index !== null ? ` row ${f.row_index}` : ""}` : "—"}
                  </td>
                  <td className="text-xs text-(--color-muted)">
                    {f.rule_id} {f.dimension ? `· ${titleCase(f.dimension)}` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={list.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      ) : null}
      {selected !== undefined ? <FindingDetailPanel key={selected} id={selected} me={me} onChanged={list.reload} onClose={() => setSelected(undefined)} /> : null}
    </div>
  );
}
