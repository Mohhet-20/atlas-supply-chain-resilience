import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Pagination } from "../../components/Pagination";
import { Needs, Notice, RecordLink, ReviewStatus, show } from "../../components/registry/shared";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { can, registryApi } from "../../lib/registryApi";
import { TYPE_LABEL } from "../../lib/registryTypes";
import { useRegistryApi, useRegistryContext } from "../../lib/useRegistry";

const LIMIT = 20;

export function MergesPage() {
  const [offset, setOffset] = useState(0);
  const fetcher = useCallback((s: AbortSignal) => registryApi.merges({ limit: LIMIT, offset }, s), [offset]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  if (loading) return <LoadingState label="Loading merge history…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data || data.items.length === 0) return <EmptyState title="No merges yet">Confirmed duplicates can be merged from the duplicate review console.</EmptyState>;
  return (
    <section aria-label="Merge history" className="panel overflow-x-auto">
      <table className="tbl">
        <caption>{data.total} merge{data.total === 1 ? "" : "s"} (immutable history)</caption>
        <thead>
          <tr>
            <th scope="col">Merge</th>
            <th scope="col">Type</th>
            <th scope="col">Survivor ← merged</th>
            <th scope="col">Status</th>
            <th scope="col">By</th>
          </tr>
        </thead>
        <tbody>
          {data.items.map((m) => (
            <tr key={m.id}>
              <td>
                <Link to={`/registry/merges/${m.id}`}>#{m.id}</Link>
                <div className="text-xs text-(--color-muted)">{new Date(m.created_at).toLocaleString()}</div>
              </td>
              <td>{TYPE_LABEL[m.entity_type]}</td>
              <td>
                <RecordLink type={m.entity_type} id={m.survivor_id}>
                  #{m.survivor_id}
                </RecordLink>{" "}
                ← <RecordLink type={m.entity_type} id={m.merged_id}>#{m.merged_id}</RecordLink>
              </td>
              <td>
                <ReviewStatus status={m.status} />
              </td>
              <td>{m.actor}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
    </section>
  );
}

export function MergeDetailPage() {
  const { id = "" } = useParams();
  const mid = Number(id);
  const { me } = useRegistryContext();
  const fetcher = useCallback((s: AbortSignal) => registryApi.merge(mid, s), [mid]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError>();
  if (loading) return <LoadingState label="Loading merge…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  const m = data;
  const admin = can(me?.role, "admin", me?.writes_enabled);

  async function compensate() {
    setBusy(true);
    setErr(undefined);
    try {
      await registryApi.compensate(m.id, reason.trim());
      reload();
    } catch (x) {
      setErr(x instanceof ApiError ? x : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/registry/merges">← Merge history</Link>
        <h2 className="m-0 text-xl font-semibold">Merge #{m.id}</h2>
        <ReviewStatus status={m.status} />
        <span className="text-xs text-(--color-muted)">{m.merge_uid}</span>
      </div>
      <section aria-label="Summary" className="panel p-4 text-sm">
        <p className="m-0">
          {TYPE_LABEL[m.entity_type]} <RecordLink type={m.entity_type} id={m.merged_id}>#{m.merged_id}</RecordLink> was merged into{" "}
          <RecordLink type={m.entity_type} id={m.survivor_id}>#{m.survivor_id}</RecordLink> by <strong>{m.actor}</strong> on {new Date(m.created_at).toLocaleString()}
          {m.authenticated ? "" : " (unauthenticated local actor)"}.
        </p>
        <p className="mt-1 mb-0 text-(--color-muted)">
          Reason: {m.reason ?? "—"} · {m.reassigned_count} link(s) reassigned · {m.dropped_count} duplicate link(s) removed (snapshots kept) · survivor version {m.survivor_version_before} → {m.survivor_version_after}
        </p>
        {m.status === "COMPENSATED" ? (
          <p className="mt-1 mb-0">
            Compensated by {m.compensated_by} on {m.compensated_at ? new Date(m.compensated_at).toLocaleString() : "—"}: {m.compensation_reason}
          </p>
        ) : null}
      </section>

      <section aria-label="Field decisions" className="panel overflow-x-auto">
        <table className="tbl">
          <caption>Field decisions taken</caption>
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Survivor was</th>
              <th scope="col">Merged was</th>
              <th scope="col">Choice</th>
              <th scope="col">Result</th>
            </tr>
          </thead>
          <tbody>
            {(m.field_decisions?.rows ?? []).map((r) => (
              <tr key={r.field}>
                <td>{titleCase(r.field)}</td>
                <td>{show(r.survivor)}</td>
                <td>{show(r.merged)}</td>
                <td>{titleCase(r.choice ?? "")}</td>
                <td>{show(r.resolved_value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {m.status === "EXECUTED" ? (
        <section aria-label="Compensation" className="panel grid gap-2 p-4">
          <h3 className="m-0 text-base font-semibold">Compensate this merge</h3>
          <p className="m-0 text-xs text-(--color-muted)">
            Restores the merged record and the original links, but only if neither record nor any moved link has changed since. Otherwise it refuses and explains why.
          </p>
          <label className="text-sm">
            Reason
            <input className="field mt-1 block w-full" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={1000} />
          </label>
          <div>
            <button type="button" className="btn" disabled={!admin || busy || reason.trim().length < 3} onClick={compensate}>
              Compensate merge
            </button>
          </div>
          {!admin ? <Needs role="admin" me={me} /> : null}
          {err ? <Notice tone="danger">{err.message}</Notice> : null}
        </section>
      ) : null}
    </div>
  );
}
