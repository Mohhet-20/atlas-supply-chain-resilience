import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { BatchStatusBadge } from "../components/IngestionBadges";
import { PageHeader } from "../components/PageHeader";
import { Pagination } from "../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { StatCard } from "../components/StatCard";
import { api, ApiError } from "../lib/api";
import { formatTimestamp, titleCase } from "../lib/format";
import type { RowOutcome } from "../lib/ingestionTypes";
import { useApi } from "../lib/useApi";

const OUTCOMES: RowOutcome[] = ["CREATED", "UPDATED", "UNCHANGED", "REJECTED", "DUPLICATE_IN_BATCH", "SKIPPED"];

export function BatchDetailPage() {
  const { batchId } = useParams();
  const id = Number(batchId);
  const valid = Number.isInteger(id) && id > 0;
  const [outcome, setOutcome] = useState<RowOutcome | "">("");
  const [offset, setOffset] = useState(0);

  const batchFetcher = useCallback(
    (s: AbortSignal) => (valid ? api.batch(id, s) : Promise.reject(new ApiError("Invalid batch id.", 404, "not_found"))),
    [id, valid],
  );
  const rowsFetcher = useCallback(
    (s: AbortSignal) => (valid ? api.batchRows(id, { limit: 25, offset, outcome: outcome || undefined }, s) : Promise.reject(new ApiError("Invalid batch id.", 404, "not_found"))),
    [id, valid, offset, outcome],
  );
  const batch = useApi(batchFetcher);
  const rows = useApi(rowsFetcher);

  if (batch.loading) return <LoadingState label="Loading batch…" />;
  if (batch.error) return <ErrorState error={batch.error} onRetry={batch.error.status === 404 ? undefined : batch.reload} />;
  const b = batch.data;
  if (!b) return null;

  return (
    <>
      <p className="mb-2 mt-0 text-sm">
        <Link to="/data">← Data operations</Link>
      </p>
      <PageHeader
        title={b.batch_uid}
        subtitle={
          <>
            {titleCase(b.entity_type)} · {b.source_format} · {b.policy === "ATOMIC" ? "all-or-nothing" : "partial"} · source{" "}
            <span className="font-mono">{b.source_namespace}</span>
          </>
        }
        actions={<BatchStatusBadge status={b.status} />}
      />
      {b.failure_reason ? (
        <p role="alert" className="panel mb-4 border-(--color-warn)/50 p-3 text-sm">
          {b.failure_reason}
        </p>
      ) : null}
      <section aria-label="Batch counts" className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-4 xl:grid-cols-7">
        <StatCard label="Rows" value={b.total_rows} />
        <StatCard label="Created" value={b.created_count} />
        <StatCard label="Updated" value={b.updated_count} />
        <StatCard label="Unchanged" value={b.unchanged_count} />
        <StatCard label="Rejected" value={b.rejected_count} />
        <StatCard label="Skipped" value={b.skipped_count} hint="valid, not applied" />
        <StatCard label="Duplicates" value={b.duplicate_count} hint="within the file" />
      </section>
      <section className="panel mb-6 p-4" aria-label="Provenance of this batch">
        <dl className="m-0 grid grid-cols-1 gap-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
          <Item k="File (display name)" v={b.file_name ?? "—"} />
          <Item k="Mapping profile" v={b.mapping_profile_ref} mono />
          <Item k="Normalization version" v={b.normalization_version} />
          <Item k="Started" v={formatTimestamp(b.started_at)} />
          <Item k="Finished" v={b.finished_at ? formatTimestamp(b.finished_at) : "—"} />
          <Item k="Size" v={`${b.content_bytes.toLocaleString("en-US")} bytes`} />
          <Item k="Content SHA-256" v={b.content_sha256} mono />
          <Item k="Idempotency fingerprint" v={b.fingerprint} mono />
          <div>
            <dt className="text-xs text-(--color-muted)">Findings</dt>
            <dd className="m-0 mt-1">
              <Link to={`/data/quality?batch=${b.id}`}>{b.finding_count} findings</Link>
            </dd>
          </div>
        </dl>
      </section>

      <label className="mb-3 flex w-fit flex-col gap-1 text-xs text-(--color-muted)">
        Row outcome
        <select
          className="field"
          value={outcome}
          onChange={(e) => {
            setOutcome(e.target.value as RowOutcome | "");
            setOffset(0);
          }}
        >
          <option value="">All</option>
          {OUTCOMES.map((o) => (
            <option key={o} value={o}>
              {titleCase(o)}
            </option>
          ))}
        </select>
      </label>
      {rows.loading ? (
        <LoadingState label="Loading rows…" />
      ) : rows.error ? (
        <ErrorState error={rows.error} onRetry={rows.reload} />
      ) : rows.data && rows.data.items.length === 0 ? (
        <EmptyState title="No rows">No row results match this filter.</EmptyState>
      ) : rows.data ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Row results</caption>
            <thead>
              <tr>
                <th scope="col">Row</th>
                <th scope="col">Line</th>
                <th scope="col">External id</th>
                <th scope="col">Outcome</th>
                <th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {rows.data.items.map((r) => (
                <tr key={r.id}>
                  <td>{r.row_index}</td>
                  <td>{r.source_line ?? "—"}</td>
                  <td className="font-mono text-xs">{r.external_id ?? "—"}</td>
                  <td>{titleCase(r.outcome)}</td>
                  <td className="text-xs">{r.message ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={rows.data.total} limit={rows.data.limit} offset={rows.data.offset} onChange={setOffset} />
        </div>
      ) : null}
    </>
  );
}

function Item({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-(--color-muted)">{k}</dt>
      <dd className={`m-0 mt-1 break-all ${mono ? "font-mono text-xs" : ""}`}>{v}</dd>
    </div>
  );
}
