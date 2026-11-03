import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { AggregateLine, SeverityBadge } from "../../components/quality/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Pagination } from "../../components/Pagination";
import { Notice } from "../../components/registry/shared";
import { titleCase } from "../../lib/format";
import { entityPath, qualityApi } from "../../lib/qualityApi";
import { useRegistryApi } from "../../lib/useRegistry";

const ORDER = ["IMPORTED", "IMPORTED_WITH_WARNINGS", "REQUIRES_REVIEW", "REJECTED", "DUPLICATE_SKIPPED", "FAILED", "NOT_APPLIED"];

export function QualityBatchPage() {
  const { id = "" } = useParams();
  const num = Number(id);
  const [cls, setCls] = useState<string>();
  const [offset, setOffset] = useState(0);
  const fetcher = useCallback((s: AbortSignal) => qualityApi.batch(num, cls, offset, s), [num, cls, offset]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);

  if (!Number.isInteger(num) || num < 1) return <EmptyState title="Unknown import batch" />;
  if (loading && !data) return <LoadingState label="Loading import quality report…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  const b = data.batch;

  return (
    <div className="space-y-4">
      <section className="panel overflow-x-auto p-4" aria-label="Import batch">
        <div className="text-xs font-semibold uppercase tracking-wider text-(--color-muted)">Import quality report</div>
        <h2 className="m-0 text-lg font-semibold break-words">{b.file_name ?? b.batch_uid}</h2>
        <p className="m-0 mt-1 text-sm text-(--color-muted)">
          {titleCase(b.entity_type)} · {titleCase(b.status)} · policy {titleCase(b.policy)} · source {b.source_namespace ?? "—"} · {b.total_rows} rows ·{" "}
          <Link to={`/data/batches/${b.id}`}>Batch details</Link> · <Link to={`/quality/findings?batch_id=${b.id}&status=ALL`}>All findings for this batch</Link>
        </p>
        {b.failure_reason ? <p className="m-0 mt-2 text-sm text-(--color-danger)">Failure reason: {b.failure_reason}</p> : null}
        <p className="m-0 mt-2 text-sm">Records touched by this batch: <AggregateLine agg={data.records_evaluated} /></p>
        <div role="group" aria-label="Row outcomes" className="mt-3 flex flex-wrap gap-2">
          <button type="button" className="btn" aria-pressed={cls === undefined} onClick={() => { setCls(undefined); setOffset(0); }}>
            All rows ({b.total_rows})
          </button>
          {ORDER.map((k) => (
            <button key={k} type="button" className="btn" aria-pressed={cls === k} disabled={(data.counts[k] ?? 0) === 0} onClick={() => { setCls(k); setOffset(0); }}>
              {titleCase(k)} ({data.counts[k] ?? 0})
            </button>
          ))}
        </div>
        {!data.all_rows_accounted_for ? <div className="mt-3"><Notice tone="warn">Row outcomes do not add up to the batch total; the batch may still be processing.</Notice></div> : null}
        <p className="mt-3 mb-0 text-xs text-(--color-muted)">{data.classification_rules} {data.raw_payload}</p>
      </section>

      {data.file_level_findings.length > 0 ? (
        <section className="panel overflow-x-auto p-4" aria-label="File-level findings">
          <h3 className="m-0 mb-2 text-sm font-semibold">File-level findings</h3>
          <ul className="m-0 list-none space-y-1 p-0 text-sm">
            {data.file_level_findings.map((f) => (
              <li key={f.id}><SeverityBadge severity={f.severity} /> {f.message}</li>
            ))}
          </ul>
        </section>
      ) : null}

      {data.rows.total === 0 ? (
        <EmptyState title="No rows in this class" />
      ) : (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Row outcomes</caption>
            <thead>
              <tr>
                <th className="num">Row</th>
                <th>Source id</th>
                <th>Outcome</th>
                <th>Findings</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.items.map((r) => (
                <tr key={r.row_index}>
                  <td className="num">{r.row_index}</td>
                  <td>{r.external_id ?? "—"}</td>
                  <td>
                    {titleCase(r.classification)}
                    {r.entity_id ? <> · <Link to={entityPath(b.entity_type, r.entity_id)}>record</Link></> : null}
                  </td>
                  <td>
                    {r.findings.length === 0 ? <span className="text-(--color-muted)">{r.message ?? "None"}</span> : (
                      <ul className="m-0 list-none space-y-1 p-0">
                        {r.findings.map((f) => (
                          <li key={f.id} className="text-sm">
                            <SeverityBadge severity={f.severity} /> {f.message}
                            {f.observed_value ? <span className="text-xs text-(--color-muted)"> (value: {f.observed_value})</span> : null}
                            {f.observed_value_redacted ? <span className="text-xs text-(--color-muted)"> (value hidden for your role)</span> : null}
                          </li>
                        ))}
                      </ul>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.rows.total} limit={data.rows.limit} offset={offset} onChange={setOffset} />
        </div>
      )}
    </div>
  );
}
