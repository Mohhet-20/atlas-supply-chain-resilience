import { useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ResolutionBadge, SeverityBadge } from "../components/IngestionBadges";
import { PageHeader } from "../components/PageHeader";
import { Pagination } from "../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { api, ApiError } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import type { Finding, ResolutionStatus, Severity } from "../lib/ingestionTypes";
import { useApi } from "../lib/useApi";

const PAGE_SIZE = 25;
const SEVERITIES: Severity[] = ["CRITICAL", "ERROR", "WARNING", "INFO"];
const STATUSES: ResolutionStatus[] = ["OPEN", "ACKNOWLEDGED", "RESOLVED", "WONT_FIX"];

export function QualityPage() {
  const [params, setParams] = useSearchParams();
  const severity = (params.get("severity") ?? "") as Severity | "";
  const status = (params.get("status") ?? "") as ResolutionStatus | "";
  const rule = params.get("rule") ?? "";
  const batchId = Number(params.get("batch") ?? 0) || undefined;
  const offset = Math.max(0, Number(params.get("offset") ?? 0) || 0);
  const [selected, setSelected] = useState<Finding | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | undefined>();

  async function exportCsv() {
    setExporting(true);
    setExportError(undefined);
    try {
      await api.exportFindings({ batch_id: batchId, severity: severity || undefined, resolution_status: status || undefined });
    } catch (e) {
      setExportError(e instanceof Error ? e.message : "Export failed.");
    } finally {
      setExporting(false);
    }
  }

  const update = useCallback(
    (changes: Record<string, string>) =>
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(changes)) {
            if (v) next.set(k, v);
            else next.delete(k);
          }
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );

  const rulesFetcher = useCallback((s: AbortSignal) => api.rules(s), []);
  const rules = useApi(rulesFetcher);
  const fetcher = useCallback(
    (signal: AbortSignal) =>
      api.findings(
        {
          limit: PAGE_SIZE,
          offset,
          severity: severity || undefined,
          resolution_status: status || undefined,
          rule_id: rule || undefined,
          batch_id: batchId,
        },
        signal,
      ),
    [offset, severity, status, rule, batchId],
  );
  const { data, error, loading, reload } = useApi(fetcher);
  const filtered = Boolean(severity || status || rule || batchId);

  return (
    <>
      <PageHeader
        title="Data quality"
        subtitle="Findings from every ingestion. They are never deleted; review changes only their resolution status."
        actions={
          <button type="button" className="btn" onClick={exportCsv} disabled={exporting}>
            {exporting ? "Exporting…" : "Export CSV"}
          </button>
        }
      />
      {exportError ? (
        <p role="alert" className="mb-4 text-sm text-(--color-danger)">
          {exportError}
        </p>
      ) : null}
      <form className="mb-4 flex flex-wrap gap-3" role="search" aria-label="Filter findings" onSubmit={(e) => e.preventDefault()}>
        <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
          Severity
          <select className="field" value={severity} onChange={(e) => update({ severity: e.target.value, offset: "" })}>
            <option value="">All</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s.toLowerCase()}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
          Review status
          <select className="field" value={status} onChange={(e) => update({ status: e.target.value, offset: "" })}>
            <option value="">All</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace("_", " ").toLowerCase()}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
          Rule
          <select className="field" value={rule} onChange={(e) => update({ rule: e.target.value, offset: "" })}>
            <option value="">All rules</option>
            {(rules.data ?? []).map((r) => (
              <option key={r.rule_id} value={r.rule_id}>
                {r.rule_id} · {r.name.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </label>
        {batchId ? (
          <button type="button" className="btn self-end" onClick={() => update({ batch: "", offset: "" })}>
            Clear batch #{batchId}
          </button>
        ) : null}
      </form>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[2fr_1fr]">
        <div className="min-w-0">
          {loading ? (
            <LoadingState label="Loading findings…" />
          ) : error ? (
            <ErrorState error={error} onRetry={reload} />
          ) : data && data.items.length === 0 ? (
            <EmptyState title={filtered ? "No findings match these filters" : "No data-quality findings"}>
              {filtered ? "Clear a filter to see more." : "Findings appear here when an ingestion detects a problem."}
            </EmptyState>
          ) : data ? (
            <div className="panel overflow-x-auto">
              <table className="tbl">
                <caption className="sr-only">Data-quality findings</caption>
                <thead>
                  <tr>
                    <th scope="col">Severity</th>
                    <th scope="col">Rule</th>
                    <th scope="col">Where</th>
                    <th scope="col">Message</th>
                    <th scope="col">Review</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((f) => (
                    <tr key={f.id} aria-selected={selected?.id === f.id}>
                      <td>
                        <SeverityBadge severity={f.severity} />
                      </td>
                      <td className="font-mono text-xs">{f.rule_id}</td>
                      <td className="text-xs">
                        <Link to={`/data/batches/${f.batch_id}`}>batch {f.batch_id}</Link>
                        {f.row_index ? ` · row ${f.row_index}` : ""}
                        {f.field ? ` · ${f.field}` : ""}
                      </td>
                      <td>
                        <button type="button" className="text-left text-(--color-accent) underline-offset-2 hover:underline" onClick={() => setSelected(f)}>
                          {f.message}
                        </button>
                      </td>
                      <td>
                        <ResolutionBadge status={f.resolution_status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <Pagination total={data.total} limit={data.limit} offset={data.offset} onChange={(o) => update({ offset: o ? String(o) : "" })} />
            </div>
          ) : null}
        </div>
        <FindingDetail key={selected?.id ?? "none"} finding={selected} onChanged={(f) => { setSelected(f); reload(); }} />
      </div>
    </>
  );
}

function FindingDetail({ finding, onChanged }: { finding: Finding | null; onChanged: (f: Finding) => void }) {
  const [note, setNote] = useState(finding?.resolution_note ?? "");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  if (!finding) {
    return (
      <aside className="panel p-4 text-sm text-(--color-muted)" aria-label="Finding details">
        Select a finding to see its explanation, source position and suggested fix.
      </aside>
    );
  }
  async function act(resolution_status: ResolutionStatus) {
    if (!finding) return;
    setBusy(true);
    setProblem(null);
    try {
      onChanged(await api.resolveFinding(finding.id, { resolution_status, note: note || undefined }));
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : "Could not update the finding.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <aside className="panel p-4 text-sm" aria-label="Finding details">
      <h2 className="m-0 mb-2 text-base font-semibold">
        {finding.rule_id} <SeverityBadge severity={finding.severity} />
      </h2>
      <p className="mt-0">{finding.message}</p>
      <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-(--color-muted)">Entity</dt>
        <dd className="m-0">{finding.entity_type ?? "—"} {finding.external_id ?? ""}</dd>
        <dt className="text-(--color-muted)">Field</dt>
        <dd className="m-0">{finding.field ?? "—"}</dd>
        <dt className="text-(--color-muted)">Position</dt>
        <dd className="m-0">{finding.row_index ? `row ${finding.row_index}${finding.source_line ? ` (line ${finding.source_line})` : ""}` : "file level"}</dd>
        <dt className="text-(--color-muted)">Observed</dt>
        <dd className="m-0 font-mono break-all">{finding.observed_value ?? "—"}</dd>
        <dt className="text-(--color-muted)">Raised</dt>
        <dd className="m-0">{formatTimestamp(finding.created_at)}</dd>
      </dl>
      {finding.suggestion ? <p className="mb-0 mt-3 rounded-md bg-(--color-ink-700) p-2 text-xs">Suggestion: {finding.suggestion}</p> : null}
      <label className="mt-4 flex flex-col gap-1 text-xs text-(--color-muted)">
        Review note
        <textarea className="field" rows={2} maxLength={400} value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      {problem ? <p role="alert" className="mt-2 text-xs text-(--color-danger)">{problem}</p> : null}
      <div className="mt-3 flex flex-wrap gap-2">
        {(["ACKNOWLEDGED", "RESOLVED", "WONT_FIX", "OPEN"] as ResolutionStatus[]).map((s) => (
          <button key={s} type="button" className="btn" disabled={busy || finding.resolution_status === s} onClick={() => void act(s)}>
            {s === "OPEN" ? "Reopen" : s === "WONT_FIX" ? "Won't fix" : s === "ACKNOWLEDGED" ? "Acknowledge" : "Resolve"}
          </button>
        ))}
      </div>
    </aside>
  );
}
